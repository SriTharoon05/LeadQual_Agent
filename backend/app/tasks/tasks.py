"""Lead qualification workers.

Each contact identity is serialized by an Azure SQL session lock. Finished
compatible results are copied before paid work. Otherwise: saved/new research,
contact verification, evidence-based ICP, final scoring, then atomic counters.
Job finalization derives terminal counts from persisted lead states.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.celery_app import celery
from app.database import db_session, locked_lead_session
from app.models import Job, Lead, ValidationResult, FinalScore
from app.utils.lead_identity import reusable_lead, copy_result, json_object
from app.utils.logger import pipeline_logger
from app.utils.company_logger import CompanyLogger

from app.validators.icp_validator import ICPResult, _s, validate_icp
from app.validators.web_researcher import research_lead
from app.validators.contact_verifier import ContactVerifyResult, verify_contact
from app.scoring.scoring_engine import score_lead
from app.utils.monitor import bind_context
# RAG ingestion hook (persist scrape + fire async embedding into Qdrant)
from app.tasks.embed_task import persist_and_dispatch_embed
from app.config import settings
import threading
import hashlib
import redis
from app.validators.web_researcher import WebResearchResult
_web_cache_lock = threading.Lock()
_web_research_cache: dict[str, object] = {}
_redis_cache = redis.Redis.from_url(
    settings.REDIS_URL, decode_responses=True,
    socket_connect_timeout=3, socket_timeout=3,
)
logger = logging.getLogger(__name__)


# ── Safe helpers ──────────────────────────────────────────────────────────────
def _num(value, default: float = 0.0) -> float:
    """Coerce to float, never raising (fixes round(None) crashes)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return default
    if isinstance(value, str):
        import re
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            try:
                return float(m.group())
            except Exception:
                return default
    return default


def _require_stage_result(result, stage: str) -> None:
    """Service/parse failures are retryable, not low-quality lead evidence."""
    payload = json_object(getattr(result, "raw_output", None))
    if (result is None or not payload or getattr(result, "error", None)
            or payload.get("error") or payload.get("fallback")):
        raise RuntimeError(f"{stage} did not produce a successful result")
    if stage == "web" and (not isinstance(payload.get("evidence"), dict)
                           or not payload["evidence"]):
        raise RuntimeError("Web research did not return structured evidence")
    if stage == "icp" and not payload.get("checks"):
        raise RuntimeError("ICP validation did not return structured checks")
    if stage == "contact" and not isinstance(payload.get("methods"), list):
        raise RuntimeError("Contact verification did not return structured observations")


def _restore_stage_result(row, result_type, stage: str):
    """Resume a previously committed successful stage after worker retries."""
    if row is None:
        return None
    try:
        payload = json_object(row.raw_output)
        result = result_type(**{key: value for key, value in payload.items()
                               if key in result_type.__dataclass_fields__})
        result.raw_output = row.raw_output
        _require_stage_result(result, stage)
        return result
    except (RuntimeError, TypeError, ValueError):
        return None


class _NullLog:
    """No-op per-company logger used if CompanyLogger construction fails —
    guarantees the pipeline never crashes on a logging problem."""
    def header(self, *a, **k): pass
    def stage(self, *a, **k): pass
    def scrape(self, *a, **k): pass
    def note(self, *a, **k): pass
    def error(self, *a, **k): pass
    def close(self, *a, **k): pass


# ── Task 1: process_lead ──────────────────────────────────────────────────────
@celery.task(
    name="app.tasks.tasks.process_lead",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def process_lead(self, lead_id: str, icp_prompt: str):
    """
    Full pipeline for a single lead:
    DB reuse check → Web → Contact → ICP using evidence → Score → DB
    """
    try:
        with locked_lead_session(lead_id) as db:
            return _process_lead(self, db, lead_id, icp_prompt)
    except TimeoutError as exc:
        # Contention does not consume the separate processing-error budget.
        raise self.retry(exc=exc, countdown=30, max_retries=self.request.retries + 1)
    except Exception as exc:
        headers = dict(self.request.headers or {})
        failures = int(headers.get("pipeline_failures", 0))
        if failures >= 3:
            with locked_lead_session(lead_id) as db:
                lead = db.get(Lead, lead_id)
                if lead:
                    _mark_lead_finished(db, lead, failed=True)
            return
        headers["pipeline_failures"] = failures + 1
        raise self.retry(exc=exc, headers=headers, countdown=30,
                         max_retries=self.request.retries + 1)


def _process_lead(self, db, lead_id, icp_prompt):
    log = pipeline_logger(db, lead_id=lead_id)

    try:
        lead: Lead = db.query(Lead).filter(Lead.id == lead_id).first()
    except Exception as e:
        log.error(f"Failed to load lead {lead_id}: {e}")
        raise
    if not lead:
        log.error(f"Lead {lead_id} not found — skipping")
        return

    log.set_job_id(lead.job_id)
    log.info(f"Processing lead: {lead.contact_name} @ {lead.company_name}")
    bind_context(job_id=lead.job_id, lead_id=lead_id, company=lead.company_name)

    if lead.processing_status in ("completed", "failed"):
        return
    if lead.final_score:
        _mark_lead_finished(db, lead, failed=False)
        return
    source, reuse_kind = reusable_lead(db, lead, icp_prompt)
    if (source and reuse_kind == "full" and lead.evidence_checked_at is not None
            and (source.evidence_checked_at is None or source.evidence_checked_at < lead.evidence_checked_at)):
        source, reuse_kind = None, None  # Resume this attempt's newer saved research.
    if source and reuse_kind == "full":
        # Clear incomplete stage rows left by a previous attempt before copying.
        db.query(ValidationResult).filter(ValidationResult.lead_id == lead.id).delete()
        copy_result(db, source, lead)
        _mark_lead_finished(db, lead, failed=False)
        return

    lead_data = _lead_to_dict(lead)

    try:
        clog = CompanyLogger(
            company_name=lead.company_name,
            contact_name=lead.contact_name,
            lead_id=lead_id,
            job_id=lead.job_id,
            icp_prompt=icp_prompt,
        )
        clog.header(lead_data)
    except Exception as e:
        logger.warning(f"CompanyLogger init failed for {lead_id}: {e}")
        clog = _NullLog()

    try:
        # ── Stage 2: Web Research (distributed identity cache) ───────────
        log.info("Stage 2: Web research")
        t0 = datetime.now(timezone.utc)
        # Contact identity is part of the key: reusing company-level data
        # across people would incorrectly reuse person_verified.
        # Reuse keys include the uploaded personal profile, not just the name.
        identity = lead.research_fingerprint or lead.id
        cache_key = "ql:web:v2:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        cached_web = None
        previous_web = db.query(ValidationResult).filter_by(lead_id=lead.id, stage="web").first()
        cached_web = _restore_stage_result(previous_web, WebResearchResult, "web")
        resuming_saved_web = cached_web is not None
        if cached_web is None and source and reuse_kind == "research":
            saved = next(v for v in source.validation_results if v.stage == "web")
            cached_web = _restore_stage_result(saved, WebResearchResult, "web")
            if cached_web is not None:
                lead.reused_from_lead_id = source.reused_from_lead_id or source.id
                lead.reuse_kind = "research"
        # Release all read/write transactions before Redis, DNS or AI calls.
        # The session-owned identity lock remains held across these commits.
        db.commit()
        if cached_web is None:
            cached_web = _get_cached_web(cache_key)

        if cached_web:
            web_result = cached_web
            log.info(f"Web research cache hit for '{lead.company_name}' — skipping LLM call")
        else:
            web_result = research_lead(lead_data,
                                       job_id=lead.job_id, lead_id=lead_id)
            _require_stage_result(web_result, "web")
            _set_cached_web(cache_key, web_result)
        _require_stage_result(web_result, "web")

        clog.stage("STAGE 2: WEB RESEARCH", web_result,
                   source=research_lead, inputs=lead_data, started=t0)
        log.info("  ↳ Stage 2 output: " + _summarise(
            web_result, ["company_verified", "person_verified",
                         "industry_confirmed", "employee_count_confirmed",
                         "technologies_found", "growth_signals", "sources"]))
        _save_validation(db, lead_id, stage="web", result=web_result,
                         score=_web_score(web_result),
                         passed=bool(getattr(web_result, "company_verified", False)),
                         sources=getattr(web_result, "sources", None))
        if not json_object(web_result.raw_output).get("error"):
            lead.evidence_checked_at = _research_checked_at(web_result.checked_at)
        db.commit()

        lead.company_description = getattr(web_result, "company_description", "") or ""
        lead.technologies_used   = getattr(web_result, "technologies_used", "") or ""
        db.commit()


        # ── RAG ingestion (best-effort) ───────────────────────────────────
        try:
            # A research-only reuse still needs this job's own saved website
            # document; chat deliberately cannot traverse another upload.
            persist_and_dispatch_embed(db, lead, web_result, clog=clog)
        except Exception as e:
            logger.warning(f"RAG ingestion skipped for {lead_id}: {e}")

        # ── Stage 3: Contact Verification ─────────────────────────────────
        log.info("Stage 3: Contact verification")
        t0 = datetime.now(timezone.utc)
        contact_result = None
        if resuming_saved_web:
            saved_contact = db.query(ValidationResult).filter_by(
                lead_id=lead_id, stage="contact").first()
            contact_result = _restore_stage_result(saved_contact, ContactVerifyResult, "contact")
        resuming_saved_contact = contact_result is not None
        db.commit()
        if contact_result is None:
            contact_result = verify_contact(lead_data, web_result, icp_prompt=icp_prompt)
        _require_stage_result(contact_result, "contact")
        clog.stage("STAGE 3: CONTACT VERIFICATION", contact_result,
                   source=verify_contact, inputs=lead_data, started=t0)
        log.info("  ↳ Stage 3 output: " + _summarise(
            contact_result, ["email_valid", "phone_valid",
                             "contact_quality_score", "email_notes",
                             "employment_confirmed"]))
        _save_validation(
            db, lead_id, stage="contact", result=contact_result,
            score=_num(getattr(contact_result, "contact_quality_score", 0)),
            passed=contact_result.contact_available)
        db.commit()

        # ── Stage 1: ICP Validation ───────────────────────────────────────
        log.info("Stage 1: ICP validation")
        t0 = datetime.now(timezone.utc)
        icp_result = None
        if resuming_saved_contact:
            saved_icp = db.query(ValidationResult).filter_by(lead_id=lead_id, stage="icp").first()
            icp_result = _restore_stage_result(saved_icp, ICPResult, "icp")
        db.commit()
        if icp_result is None:
            icp_result = validate_icp(lead_data, icp_prompt,
                           job_id=lead.job_id, lead_id=lead_id,
                           evidence=web_result.evidence, contact_result=contact_result)
        _require_stage_result(icp_result, "icp")
        clog.stage("STAGE 1: ICP VALIDATION", icp_result,
                   source=validate_icp, inputs=lead_data, started=t0)
        log.info("  ↳ Stage 1 output: " + _summarise(
            icp_result, ["icp_match", "icp_score", "icp_reasoning",
                         "decision_maker_reasoning"]))
        _save_validation(db, lead_id, stage="icp", result=icp_result,
                         score=_num(getattr(icp_result, "icp_score", 0)),
                         passed=bool(getattr(icp_result, "icp_match", False)))
        db.commit()

        # ── Scoring ───────────────────────────────────────────────────────
        log.info("Scoring")

        existing_fs = db.query(FinalScore).filter(
            FinalScore.lead_id == lead_id).first()
        if existing_fs:
            log.info(f"Score already exists for lead {lead_id} — skipping (retry guard)")
            _mark_lead_finished(db, lead, failed=False)
            clog.close(existing_fs)
            return

        lead_data["Company Description"] = lead.company_description or ""
        lead_data["Technologies Used"]   = lead.technologies_used or ""

        db.commit()
        t0 = datetime.now(timezone.utc)
        score_result = score_lead(lead_data, icp_result, web_result,
                                  contact_result,
                                  job_id=lead.job_id, lead_id=lead_id, icp_prompt=icp_prompt)
        _require_stage_result(score_result, "score")
        clog.stage("SCORING", score_result, source=score_lead, started=t0)

        fs = FinalScore(
            lead_id=         lead_id,
            total_score=     _num(getattr(score_result, "total_score", 0)),
            icp_score=       _num(getattr(score_result, "icp_score", 0)),
            authority_score=None, data_score=None,  # Inactive legacy storage only.
            growth_score=    _num(getattr(score_result, "growth_score", 0)),
            contact_score=   _num(getattr(score_result, "contact_score", 0)),
            scoring_version=score_result.scoring_version,
            qualification_status=score_result.qualification_status,
            scoring_details_json=score_result.scoring_details_json,
            priority=        getattr(score_result, "priority", "Low") or "Low",
            reasoning=       getattr(score_result, "reasoning", "") or "",
            growth_signals=  getattr(score_result, "growth_signals", "") or "",
            outreach_intelligence=getattr(score_result, "outreach_intelligence", "") or "",
        )
        db.add(fs)
        db.flush()

        log.info(
            "  ↳ Score breakdown: "
            f"icp={fs.icp_score} growth={fs.growth_score} "
            f"contact={fs.contact_score}")
        log.info(
            f"Done: score={fs.total_score} priority={fs.priority} — "
            f"{_trunc(fs.reasoning, 140)}")

        clog.close(score_result)
        _mark_lead_finished(db, lead, failed=False)

    except Exception as exc:
        log.error(f"Pipeline error for {lead_id}: {exc}")
        try:
            clog.error("Pipeline error", exc=exc)
        except Exception:
            pass
        raise

# ── Task 2: finalise_job ──────────────────────────────────────────────────────
@celery.task(name="app.tasks.tasks.finalise_job", bind=True, max_retries=1)
def finalise_job(self, results: list, job_id: str):
    with db_session() as db:
        log = pipeline_logger(db, job_id=job_id)
        try:
            job: Job = db.query(Job).filter(Job.id == job_id).first()
        except Exception as e:
            log.error(f"finalise_job: failed to load job {job_id}: {e}")
            return
        if not job:
            log.error(f"Job {job_id} not found in finalise_job")
            return

        total = db.query(Lead).filter(Lead.job_id == job_id).count()
        processed = db.query(Lead).filter(Lead.job_id == job_id,
            Lead.processing_status.in_(("completed", "failed"))).count()
        failed = db.query(Lead).filter(Lead.job_id == job_id,
            Lead.processing_status == "failed").count()
        job.processed_rows, job.failed_rows = processed, failed
        if processed < total:
            job.status = "running"
            raise self.retry(countdown=30, max_retries=self.request.retries + 1)

        # Aggregate token totals from llm_call_logs for this job
        try:
            from sqlalchemy import func as sqlfunc
            from app.models import LLMCallLog
            row = (
                db.query(
                    sqlfunc.sum(LLMCallLog.prompt_tokens).label("p"),
                    sqlfunc.sum(LLMCallLog.completion_tokens).label("c"),
                    sqlfunc.sum(LLMCallLog.total_tokens).label("t"),
                )
                .filter(LLMCallLog.job_id == job_id)
                .first()
            )
            if row:
                job.prompt_tokens     = int(row._mapping["p"] or 0)
                job.completion_tokens = int(row._mapping["c"] or 0)
                job.total_tokens_used = int(row._mapping["t"] or 0)
                log.info(
                    f"Token totals — prompt={job.prompt_tokens} "
                    f"completion={job.completion_tokens} "
                    f"total={job.total_tokens_used}"
                )
        except Exception as e:
            logger.warning(f"Token rollup failed for job {job_id}: {e}")

        # Record which models were used
        try:
            job.llm_model   = settings.AZURE_OPENAI_DEPLOYMENT
            job.embed_model = settings.EMBED_DEPLOYMENT
        except Exception as e:
            logger.warning(f"Model name capture failed for job {job_id}: {e}")

        # If literally nothing was processed, mark failed rather than pretend OK.
        job.status = "completed" if processed > failed else "failed"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()

        log.info(f"Job finalised ({job.status}): {processed}/{total} leads processed")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _trunc(value, limit: int = 100) -> str:
    if value is None:
        return "∅"
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _summarise(result, fields: list[str]) -> str:
    parts = []
    for field in fields:
        try:
            if not hasattr(result, field):
                continue
            val = getattr(result, field)
        except Exception:
            continue
        if val is None or val == "":
            continue
        if isinstance(val, str) and len(val) > 80:
            val = _trunc(val, 80)
        parts.append(f"{field}={val}")
    return " | ".join(parts) if parts else "(no fields available)"


def _lead_to_dict(lead: Lead) -> dict:
    """Convert a Lead ORM object to the canonical column dict (None-safe)."""
    field_map = {
        "Contact Name":        lead.contact_name,
        "Job Title":           lead.job_title,
        "Contact Department":  lead.contact_department,
        "Contact Seniority":   lead.contact_seniority,
        "LinkedIn Profile URL":lead.linkedin_profile_url,
        "Email Address":       lead.email_address,
        "Phone Number":        lead.phone_number,
        "Direct Phone Number": lead.direct_phone_number,
        "Contact Country":     lead.contact_country,
        "Contact State/Region":lead.contact_state_region,
        "Contact City":        lead.contact_city,
        "Company Name":        lead.company_name,
        "Company Website":     lead.company_website,
        "Company LinkedIn URL":lead.company_linkedin_url,
        "Industry":            lead.industry,
        "Sub Industry":        lead.sub_industry,
        "Employee Count":      lead.employee_count,
        "Annual Revenue":      lead.annual_revenue,
        "Company Description": lead.company_description,
        "Technologies Used":   lead.technologies_used,
        "Company Country":     lead.company_country,
        "Company State/Region":lead.company_state_region,
        "Company City":        lead.company_city,
        "Company HQ Location": lead.company_hq_location,
        "HQ Phone Number":     lead.hq_phone_number,
        "CRM Account ID":      lead.crm_account_id,
        "CRM Contact ID":      lead.crm_contact_id,
        "Lead Source":         lead.lead_source,
        "Notes":               lead.notes,
    }
    return {k: (v or "") for k, v in field_map.items()}


def _save_validation(
    db: Session, lead_id: str, stage: str, result,
    score: float, passed: bool, sources=None,
) -> None:
    """Persist a stage result. None-safe on score + sources. Never raises hard."""
    try:
        vr = db.query(ValidationResult).filter(
            ValidationResult.lead_id == lead_id,
            ValidationResult.stage == stage,
        ).first() or ValidationResult(lead_id=lead_id, stage=stage)
        vr.passed = bool(passed)
        vr.score = round(_num(score), 2)
        vr.reasoning = (getattr(result, "decision_maker_reasoning", None)
                        or getattr(result, "growth_signals", None)
                        or getattr(result, "email_notes", None)
                        or "")
        vr.raw_output = getattr(result, "raw_output", "") or ""
        vr.sources_json = json.dumps(sources) if sources else None
        if vr.id is None:
            db.add(vr)
        db.flush()
    except Exception as e:
        logger.warning(f"_save_validation({stage}) failed for {lead_id}: {e}")
        raise


def _web_score(web_result) -> float:
    """Convert Stage 2 boolean fields to a 0–10 score (attribute-safe)."""
    try:
        growth = getattr(web_result, "growth_signals", "") or ""
        checks = [
            bool(getattr(web_result, "company_verified", False)),
            bool(getattr(web_result, "person_verified", False)),
            bool(getattr(web_result, "industry_confirmed", False)),
            bool(getattr(web_result, "employee_count_confirmed", False)),
            bool(getattr(web_result, "technologies_found", None)),
            bool(growth and "none" not in str(growth).lower()),
        ]
        return round((sum(checks) / len(checks)) * 10, 1)
    except Exception:
        return 0.0


def _mark_lead_finished(db: Session, lead: Lead, failed: bool) -> None:
    """Idempotently finish a lead and advance its job in the same transaction."""
    if getattr(lead, "processing_status", "pending") in ("completed", "failed"):
        return
    from sqlalchemy import update
    lead.processing_status = "failed" if failed else "completed"
    values = {"processed_rows": Job.processed_rows + 1}
    if failed:
        values["failed_rows"] = Job.failed_rows + 1
    elif lead.reuse_kind == "full":
        values["reused_rows"] = Job.reused_rows + 1
    elif lead.reuse_kind == "research":
        values["research_reused_rows"] = Job.research_reused_rows + 1
    db.execute(update(Job).where(Job.id == lead.job_id).values(**values))
    db.flush()


def _get_cached_web(key: str):
    try:
        payload = _redis_cache.get(key)
        if payload:
            data = json.loads(payload)
            if not isinstance(data, dict) or not data.get("evidence") or not data.get("checked_at"):
                return None
            result = WebResearchResult(**data)
            result.raw_output = json.dumps(data, default=str)
            return result
    except Exception as exc:
        logger.debug("Redis web cache read unavailable: %s", exc)
    with _web_cache_lock:
        return _web_research_cache.get(key)


def _research_checked_at(value):
    """Preserve the search's actual check time when saved research is reused."""
    try:
        checked = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return checked.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _set_cached_web(key: str, result) -> None:
    if not result.evidence or not result.checked_at:
        return
    try:
        payload = result.to_dict()
        _redis_cache.setex(key, 86400, json.dumps(payload, default=str))
        return
    except Exception as exc:
        logger.debug("Redis web cache write unavailable: %s", exc)
    with _web_cache_lock:
        # Bound fallback memory in workers when Redis cache commands fail.
        if len(_web_research_cache) >= 1000:
            _web_research_cache.clear()
        _web_research_cache[key] = result
