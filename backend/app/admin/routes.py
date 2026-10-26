"""Admin runtime settings, paginated cost drilldowns and warning/error feeds."""
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.request import urlopen
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import BigInteger, Numeric, and_, case, cast, func, or_, update
from sqlalchemy.orm import Session, undefer

from app.admin.concurrency import active_count
from app.admin.settings import DEFAULT_PRICING, cache_settings, redis_client, settings_dict, utcnow
from app.admin.telemetry import safe_message
from app.config import settings
from app.database import get_db
from app.models import Job, Lead, LLMCallLog, OperationalEvent, RuntimeSettings
from app.utils.db_availability import database_unavailable_reason


def require_admin(request: Request):
    # All application logins are issued from admin_users. Team-directory entries
    # never created login accounts. AuthenticationMiddleware verifies the JWT.
    if not getattr(request.state, "username", None):
        raise HTTPException(401, "Administrator authentication required")


router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


class PricingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input_per_million: float = Field(ge=0, le=10000)
    cached_per_million: float = Field(ge=0, le=10000)
    output_per_million: float = Field(ge=0, le=10000)
    embedding_per_million: float = Field(ge=0, le=10000)
    search_per_thousand: float = Field(ge=0, le=10000)
    confirmed: bool = False


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    concurrency: StrictInt = Field(ge=1, le=30)
    version: StrictInt = Field(ge=0)
    pricing: PricingUpdate | None = None
    usd_inr: float | None = Field(default=None, gt=0, le=10000)


def runtime_row(db):
    row = db.get(RuntimeSettings, "runtime", populate_existing=True)
    if row is None:
        raise HTTPException(503, "Run migrate_admin_operations.py before using admin controls")
    return row


def runtime_status(value):
    result = {**value, "max_capacity": 30, "active": None, "workers": [],
              "redis_available": False, "telemetry_pending": None,
              "cooldown_seconds": 0,
              "deployment": settings.AZURE_OPENAI_DEPLOYMENT, "embedding_deployment": settings.EMBED_DEPLOYMENT}
    try:
        client = redis_client()
        result["active"] = active_count()
        keys = []
        for key in client.scan_iter(match="ql:runtime:worker:*", count=100):
            keys.append(key)
            if len(keys) >= 100:
                break
        result["workers"] = [json.loads(item) for item in client.mget(keys) if item] if keys else []
        result["telemetry_pending"] = client.xlen("ql:telemetry:outbox:v1")
        result["cooldown_seconds"] = max(0, client.ttl("ql:runtime:provider-cooldown"))
        result["redis_available"] = True
    except Exception:
        pass
    return result


@router.get("/admin/settings")
def get_settings(db: Session = Depends(get_db)):
    value = settings_dict(runtime_row(db))
    db.commit()
    return runtime_status(value)


@router.put("/admin/settings")
def save_settings(body: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    row = runtime_row(db)
    values = {"concurrency": body.concurrency, "version": body.version + 1,
              "updated_at": utcnow(), "updated_by": request.state.username}
    if body.pricing is not None:
        pricing = json.loads(row.pricing_json)
        pricing.update(body.pricing.model_dump())
        values["pricing_json"] = json.dumps(pricing)
    if "usd_inr" in body.model_fields_set and body.usd_inr != row.usd_inr:
        values.update(usd_inr=body.usd_inr, fx_as_of=utcnow() if body.usd_inr else None,
                      fx_source="Manually configured reference rate" if body.usd_inr else None)
    changed = db.execute(update(RuntimeSettings).where(RuntimeSettings.id == "runtime",
        RuntimeSettings.version == body.version).values(**values)).rowcount
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "Settings changed elsewhere. Reload saved settings before saving again.")
    db.commit()
    db.expire_all()
    value = settings_dict(runtime_row(db))
    db.commit()
    try:
        cache_settings(value)
    except Exception:
        pass  # The SQL save succeeded; admission fails closed until Redis returns.
    return runtime_status(value)


def refresh_fx(db, *, automatic=False, actor=None):
    row = runtime_row(db)
    initial = settings_dict(row)
    if automatic and initial["usd_inr"] is not None:
        return initial
    if automatic:
        try:
            if not redis_client().set("ql:fx:fetch-attempt", "1", nx=True, ex=3600):
                return initial
        except Exception:
            return initial
    db.commit()  # Never hold a SQL connection while fetching a reference rate.
    try:
        with urlopen("https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR", timeout=4) as response:
            payload = json.loads(response.read(32768).decode("utf-8"))
        rate = float(payload["rates"]["INR"])
        as_of = datetime.fromisoformat(payload["date"])
        if not math.isfinite(rate) or not 0 < rate <= 10000 or payload.get("base") != "USD":
            raise ValueError("Invalid exchange-rate response")
    except Exception:
        if automatic:
            return initial
        raise HTTPException(502, "Reference rate unavailable. Retry or enter USD/INR manually.") from None
    values = {"usd_inr": rate, "fx_as_of": as_of, "fx_source": "https://frankfurter.dev/",
              "version": initial["version"] + 1, "updated_at": utcnow(),
              "updated_by": actor or "exchange-rate service"}
    changed = db.execute(update(RuntimeSettings).where(RuntimeSettings.id == "runtime",
        RuntimeSettings.version == initial["version"]).values(**values)).rowcount
    if changed != 1:
        db.rollback()
        if not automatic:
            raise HTTPException(409, "Settings changed during the rate refresh. Reload and retry.")
    else:
        db.commit()
    db.expire_all()
    result = settings_dict(runtime_row(db))
    db.commit()
    try:
        cache_settings(result)
    except Exception:
        pass
    return result


@router.post("/admin/settings/exchange-rate")
def exchange_rate(request: Request, db: Session = Depends(get_db)):
    return runtime_status(refresh_fx(db, actor=request.state.username))


def _cost_columns(pricing):
    log = LLMCallLog
    legacy = log.status.is_(None)
    known = or_(func.lower(log.model).like("gpt-4.1-mini%"), func.lower(log.model).like("text-embedding-ada-002%"))
    embed = func.lower(log.model).like("text-embedding-ada-002%")
    old_input = cast(log.prompt_tokens * case((embed, Decimal(str(pricing["embedding_per_million"]))),
        else_=Decimal(str(pricing["input_per_million"]))) / Decimal(1000000), Numeric(20, 10))
    old_output = cast(log.completion_tokens * Decimal(str(pricing["output_per_million"])) / Decimal(1000000), Numeric(20, 10))
    input_cost = case((and_(legacy, known), old_input), else_=log.cost_input_usd)
    output_cost = case((and_(legacy, known), old_output), else_=log.cost_output_usd)
    cached_cost = case((legacy, 0), else_=log.cost_cached_usd)
    search_cost = case((and_(legacy, log.use_web_search == False), 0), else_=log.cost_search_usd)
    total = sum(func.coalesce(value, 0) for value in (input_cost, cached_cost, output_cost, search_cost))
    return [func.count(log.id).label("calls"),
        func.coalesce(func.sum(cast(log.prompt_tokens, BigInteger)), 0).label("input_tokens"),
        func.coalesce(func.sum(cast(log.cached_prompt_tokens, BigInteger)), 0).label("cached_tokens"),
        func.coalesce(func.sum(cast(log.completion_tokens, BigInteger)), 0).label("output_tokens"),
        func.coalesce(func.sum(cast(log.search_requests, BigInteger)), 0).label("search_requests"),
        func.coalesce(func.sum(cast(log.web_search_calls, BigInteger)), 0).label("tool_items"),
        func.coalesce(func.sum(case((and_(log.use_web_search == True, log.search_requests.is_(None)), 1), else_=0)), 0).label("missing_search_usage"),
        func.coalesce(func.sum(case((or_(legacy, log.cost_complete == False, log.cost_complete.is_(None)), 1), else_=0)), 0).label("incomplete_calls"),
        func.coalesce(func.sum(case((legacy, 1), else_=0)), 0).label("legacy_calls"),
        func.coalesce(func.sum(case((or_(and_(legacy, or_(log.model.is_(None), ~known, log.prompt_tokens.is_(None), log.completion_tokens.is_(None))),
            and_(~legacy, or_(log.cost_input_usd.is_(None), log.cost_output_usd.is_(None)))), 1), else_=0)), 0).label("unpriced_calls"),
        func.coalesce(func.sum(case((log.status.in_(("error", "rate_limited", "usage_missing", "incomplete", "started")), 1), else_=0)), 0).label("failed_or_unreported_calls"),
        func.coalesce(func.sum(input_cost), 0).label("input_usd"),
        func.coalesce(func.sum(cached_cost), 0).label("cached_usd"),
        func.coalesce(func.sum(output_cost), 0).label("output_usd"),
        func.coalesce(func.sum(search_cost), 0).label("search_usd"),
        func.coalesce(func.sum(total), 0).label("total_usd")]


METRIC_NAMES = ("calls", "input_tokens", "cached_tokens", "output_tokens", "search_requests", "tool_items",
    "missing_search_usage", "incomplete_calls", "legacy_calls", "unpriced_calls", "failed_or_unreported_calls",
    "input_usd", "cached_usd", "output_usd", "search_usd", "total_usd")


def _numbers(row, fx):
    values = dict(row if isinstance(row, dict) else row._mapping)
    for key, value in list(values.items()):
        if isinstance(value, Decimal):
            values[key] = float(value)
        elif isinstance(value, UUID):
            values[key] = str(value)
    for key in METRIC_NAMES:
        if key in values and values[key] is None:
            values[key] = 0
    if "total_usd" in values:
        values["total_inr"] = float(values["total_usd"] or 0) * fx if fx else None
    return values


def _stage_summary(rows, fx):
    # One aggregate snapshot supplies both the breakdown and its total.
    return _numbers({key: sum(row[key] for row in rows) for key in METRIC_NAMES}, fx)


def _job(db, job_id):
    job = db.get(Job, str(job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/admin/cost")
@router.get("/admin/cost/jobs")
def cost_jobs(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
              search: str = Query("", max_length=100), db: Session = Depends(get_db)):
    cfg = refresh_fx(db, automatic=True)
    query = db.query(Job.id.label("job_id"), Job.file_name, Job.status, Job.total_rows, Job.created_at)
    if search.strip():
        query = query.filter(Job.file_name.contains(search.strip(), autoescape=True))
    total = query.count()
    rows = query.order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit).all()
    ids = [str(row.job_id) for row in rows]
    costs = db.query(LLMCallLog.job_id, *_cost_columns(cfg["pricing"])).filter(
        LLMCallLog.job_id.in_(ids)).group_by(LLMCallLog.job_id).all() if ids else []
    by_job = {str(row.job_id): _numbers(row, cfg["usd_inr"]) for row in costs}
    items = []
    for row in rows:
        item = dict(row._mapping)
        item["job_id"] = str(item["job_id"])
        item.update(by_job.get(item["job_id"], {name: 0 for name in METRIC_NAMES}))
        item["total_inr"] = item["total_usd"] * cfg["usd_inr"] if cfg["usd_inr"] else None
        items.append(item)
    unallocated = _numbers(db.query(*_cost_columns(cfg["pricing"])).filter(LLMCallLog.job_id.is_(None)).one(), cfg["usd_inr"])
    return {"items": items, "total": total, "pricing": cfg["pricing"], "usd_inr": cfg["usd_inr"],
            "fx_as_of": cfg["fx_as_of"], "fx_source": cfg["fx_source"], "estimated": True,
            "unallocated": unallocated}



@router.get("/admin/cost/jobs/{job_id}")
def cost_job(job_id: UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
             db: Session = Depends(get_db)):
    cfg = settings_dict(runtime_row(db))
    job = _job(db, job_id)
    query = db.query(LLMCallLog).filter(LLMCallLog.job_id == str(job_id))
    stages = [_numbers(row, cfg["usd_inr"]) for row in query.with_entities(LLMCallLog.stage,
        LLMCallLog.module, *_cost_columns(cfg["pricing"])).group_by(LLMCallLog.stage, LLMCallLog.module)
        .order_by(LLMCallLog.stage, LLMCallLog.module).all()]
    summary = _stage_summary(stages, cfg["usd_inr"])
    leads_query = db.query(Lead.id.label("lead_id"), Lead.row_index, Lead.company_name, Lead.contact_name).filter(Lead.job_id == str(job_id))
    count_leads = leads_query.count()
    page = leads_query.order_by(Lead.row_index, Lead.id).offset(offset).limit(limit).all()
    ids = [str(row.lead_id) for row in page]
    costs = query.with_entities(LLMCallLog.lead_id, *_cost_columns(cfg["pricing"])).filter(
        LLMCallLog.lead_id.in_(ids)).group_by(LLMCallLog.lead_id).all() if ids else []
    by_lead = {str(row.lead_id): _numbers(row, cfg["usd_inr"]) for row in costs}
    leads = []
    for row in page:
        item = dict(row._mapping)
        item["lead_id"] = str(item["lead_id"])
        item.update(by_lead.get(item["lead_id"], {name: 0 for name in METRIC_NAMES}))
        item["total_inr"] = item["total_usd"] * cfg["usd_inr"] if cfg["usd_inr"] else None
        leads.append(item)
    job_level = _numbers(query.filter(LLMCallLog.lead_id.is_(None)).with_entities(*_cost_columns(cfg["pricing"])).one(), cfg["usd_inr"])
    return {"job_id": str(job_id), "file_name": job.file_name, "summary": summary, "by_stage": stages,
        "job_level": job_level, "leads": leads, "total_leads": count_leads,
        "pricing": cfg["pricing"], "usd_inr": cfg["usd_inr"], "fx_as_of": cfg["fx_as_of"]}


@router.get("/admin/cost/jobs/{job_id}/leads/{lead_id}")
def cost_lead(job_id: UUID, lead_id: UUID, limit: int = Query(50, ge=1, le=100),
              offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    cfg = settings_dict(runtime_row(db))
    lead = db.query(Lead.id, Lead.company_name, Lead.contact_name).filter(Lead.id == str(lead_id), Lead.job_id == str(job_id)).first()
    if not lead:
        raise HTTPException(404, "Lead not found in this job")
    query = db.query(LLMCallLog).filter(LLMCallLog.job_id == str(job_id), LLMCallLog.lead_id == str(lead_id))
    stages = [_numbers(row, cfg["usd_inr"]) for row in query.with_entities(LLMCallLog.stage,
        LLMCallLog.module, *_cost_columns(cfg["pricing"])).group_by(LLMCallLog.stage, LLMCallLog.module).order_by(LLMCallLog.stage, LLMCallLog.module).all()]
    summary = _stage_summary(stages, cfg["usd_inr"])
    calls = []
    for row in query.options(undefer(LLMCallLog.pricing_snapshot_json)).order_by(LLMCallLog.created_at.desc(), LLMCallLog.id.desc()).offset(offset).limit(limit).all():
        calls.append({name: getattr(row, name) for name in ("id", "stage", "module", "model", "billing_model",
            "prompt_tokens", "cached_prompt_tokens", "completion_tokens", "web_search_calls", "search_requests",
            "status", "error_code", "duration_ms", "provider_request_id", "cost_input_usd", "cost_cached_usd",
            "cost_output_usd", "cost_search_usd", "cost_total_usd", "cost_complete", "created_at")}
            | {"pricing_snapshot": json.loads(row.pricing_snapshot_json) if row.pricing_snapshot_json else None})
    return {"lead_id": str(lead_id), "company_name": lead.company_name, "contact_name": lead.contact_name,
        "summary": summary, "by_stage": stages, "calls": calls, "total_calls": query.count(), "usd_inr": cfg["usd_inr"]}


def event_rows(db, job_id=None, severity=None, rate_only=False, before_id=None, limit=100, system_since=None):
    query = db.query(OperationalEvent, Job.file_name, Lead.company_name, Lead.contact_name).outerjoin(
        Job, Job.id == OperationalEvent.job_id).outerjoin(Lead, Lead.id == OperationalEvent.lead_id)
    if job_id:
        predicate = OperationalEvent.job_id == str(job_id)
        if system_since is not None:
            predicate = or_(predicate, and_(OperationalEvent.job_id.is_(None), OperationalEvent.occurred_at >= system_since))
        query = query.filter(predicate)
    if severity:
        query = query.filter(OperationalEvent.severity == severity)
    if rate_only:
        query = query.filter(OperationalEvent.kind == "rate_limit")
    if before_id:
        query = query.filter(OperationalEvent.id < before_id)
    rows = query.order_by(OperationalEvent.id.desc()).limit(limit + 1).all()
    items = [{"id": event.id, "event_id": event.event_id, "job_id": str(event.job_id) if event.job_id else None,
        "lead_id": str(event.lead_id) if event.lead_id else None, "severity": event.severity, "kind": event.kind,
        "stage": event.stage, "component": event.component, "message": safe_message(event.message),
        "retry_after": event.retry_after, "occurred_at": event.occurred_at.isoformat() + "Z",
        "file_name": filename, "company_name": company, "contact_name": contact}
        for event, filename, company, contact in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit, "before_id": items[-1]["id"] if items else None, "source": "sql"}


def buffered_events(job_id=None, severity=None, rate_only=False, limit=100, include_system=False):
    try:
        key = "ql:events:job:" + str(job_id) if job_id else "ql:events:recent:v1"
        rows = [json.loads(item) for item in redis_client().lrange(key, 0, 499)]
        if include_system:
            system = [json.loads(item) for item in redis_client().lrange("ql:events:recent:v1", 0, 499)]
            rows += [item for item in system if not item.get("job_id")]
            rows = sorted({item["event_id"]: item for item in rows}.values(), key=lambda item: item["occurred_at"], reverse=True)
        items = [item for item in rows if (not severity or item["severity"] == severity)
                 and (not rate_only or item["kind"] == "rate_limit")][:limit]
        return {"items": items, "has_more": False, "before_id": None, "source": "buffered",
                "warning": "SQL is unavailable. Showing recent buffered events; full history returns after recovery."}
    except Exception:
        raise HTTPException(503, "Diagnostics storage is unavailable. Check API/worker logs.") from None


@router.get("/admin/logs")
def admin_logs(job_id: UUID | None = None, severity: str | None = Query(None, pattern="^(WARNING|ERROR)$"),
               rate_only: bool = False, before_id: int | None = Query(None, ge=1),
               limit: int = Query(100, ge=1, le=200), db: Session = Depends(get_db)):
    try:
        return event_rows(db, job_id, severity, rate_only, before_id, limit)
    except Exception as exc:
        db.rollback()
        if database_unavailable_reason(exc):
            return buffered_events(job_id, severity, rate_only, limit)
        raise


@router.get("/admin/jobs/{job_id}/events")
def job_events(job_id: UUID, limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
    try:
        job = _job(db, job_id)
        result = event_rows(db, job_id=job_id, limit=limit, system_since=job.created_at - timedelta(minutes=5))
        result["runtime"] = runtime_hint(db)
        return result
    except Exception as exc:
        db.rollback()
        if database_unavailable_reason(exc):
            return {**buffered_events(job_id=job_id, limit=limit, include_system=True), "runtime": runtime_hint()}
        raise


def runtime_hint(db=None):
    result = {"concurrency": None, "active": None, "cooldown_seconds": 0}
    if db is not None:
        result["concurrency"] = db.query(RuntimeSettings.concurrency).filter(RuntimeSettings.id == "runtime").scalar()
    try:
        client = redis_client()
        result["active"] = active_count()
        result["cooldown_seconds"] = max(0, client.ttl("ql:runtime:provider-cooldown"))
        if result["concurrency"] is None:
            cached = client.get("ql:runtime:settings:v1")
            result["concurrency"] = json.loads(cached)["concurrency"] if cached else None
    except Exception:
        pass
    return result
