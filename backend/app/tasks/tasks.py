"""
tasks/tasks.py
───────────────
Celery task definitions for the lead processing pipeline. (HARDENED edition)

Two tasks:
  1. process_lead(lead_id, icp_prompt)
     — Full Stage 1 → 2 → AI Enrich → 3 → Score → DB write pipeline
     — One task instance per lead, dispatched in parallel
     — Each stage is individually guarded so a single stage failure degrades
       to a safe default rather than aborting the whole lead.
     — Per-company debug log (raw JSON of every stage) to Logs/.
     — After enrichment, persists the scrape + dispatches async embedding.

  2. finalise_job(job_id)
     — Chord callback after all leads processed.
     — Marks the job completed (or 'failed' if NOTHING was processed).

Dispatch (from FastAPI route, after leads are inserted):
    header   = [process_lead.s(lead.id, icp_prompt) for lead in leads]
    callback = finalise_job.s(job_id)
    chord(header)(callback)
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.celery_app import celery
from app.database import db_session
from app.models import Job, Lead, ValidationResult, FinalScore
from app.utils.logger import pipeline_logger
from app.utils.company_logger import CompanyLogger

from app.validators.icp_validator import _s, validate_icp
from app.validators.web_researcher import research_lead
from app.validators.contact_verifier import verify_contact
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
_redis_cache = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
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
    Stage 1 (ICP) → Stage 2 (Web) → AI Enrich → Stage 3 (Contact) → Score → DB
    """
    with db_session() as db:
        log = pipeline_logger(db, lead_id=lead_id)

        try:
            lead: Lead = db.query(Lead).filter(Lead.id == lead_id).first()
        except Exception as e:
            log.error(f"Failed to load lead {lead_id}: {e}")
            return
        if not lead:
            log.error(f"Lead {lead_id} not found — skipping")
            return

        log.set_job_id(lead.job_id)
        log.info(f"Processing lead: {lead.contact_name} @ {lead.company_name}")
        bind_context(job_id=lead.job_id, lead_id=lead_id, company=lead.company_name)

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
            # ── Stage 1: ICP Validation ───────────────────────────────────────
            log.info("Stage 1: ICP validation")
            t0 = datetime.now(timezone.utc)
            icp_result = validate_icp(lead_data, icp_prompt,
                           job_id=lead.job_id, lead_id=lead_id)
            clog.stage("STAGE 1: ICP VALIDATION", icp_result,
                       source=validate_icp, inputs=lead_data, started=t0)
            log.info("  ↳ Stage 1 output: " + _summarise(
                icp_result, ["icp_match", "icp_score", "icp_reasoning",
                             "decision_maker_reasoning"]))
            _save_validation(db, lead_id, stage="icp", result=icp_result,
                             score=_num(getattr(icp_result, "icp_score", 0)),
                             passed=bool(getattr(icp_result, "icp_match", False)))
            db.commit()

            # ── Stage 2: Web Research (distributed identity cache) ───────────
            log.info("Stage 2: Web research")
            t0 = datetime.now(timezone.utc)
            # Contact identity is part of the key: reusing company-level data
            # across people would incorrectly reuse person_verified.
            identity = "|".join((_s(lead.company_name), _s(lead.company_website),
                                 _s(lead.contact_name), _s(lead.job_title))).lower()
            cache_key = "ql:web:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            cached_web = _get_cached_web(cache_key)

            if cached_web:
                web_result = cached_web
                log.info(f"Web research cache hit for '{lead.company_name}' — skipping LLM call")
            else:
                web_result = research_lead(lead_data,
                                           job_id=lead.job_id, lead_id=lead_id)
                _set_cached_web(cache_key, web_result)

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
            db.commit()

            # # ── AI Enrichment ─────────────────────────────────────────────────
            # log.info("AI enrichment: cols 19 & 20")
            # t0 = datetime.now(timezone.utc)
            # enriched = enrich_lead(lead_data, web_result,
            #                        job_id=lead.job_id, lead_id=lead_id)
            # if not isinstance(enriched, dict):
            #     enriched = {}
            # lead.company_description = enriched.get("company_description", "") or ""
            # lead.technologies_used   = enriched.get("technologies_used", "") or ""
            # db.flush()
            # clog.stage("AI ENRICHMENT (cols 19 & 20)", enriched,
            #            source=enrich_lead, started=t0)
            # log.info("  ↳ Enrichment output: "
            #          f"description={_trunc(enriched.get('company_description'))} | "
            #          f"technologies={_trunc(enriched.get('technologies_used'))}")

            lead.company_description = getattr(web_result, "company_description", "") or ""
            lead.technologies_used   = getattr(web_result, "technologies_used", "") or ""
            db.commit()


            # ── RAG ingestion (best-effort) ───────────────────────────────────
            try:
                persist_and_dispatch_embed(db, lead, web_result, clog=clog)
            except Exception as e:
                logger.warning(f"RAG ingestion skipped for {lead_id}: {e}")

            # ── Stage 3: Contact Verification ─────────────────────────────────
            log.info("Stage 3: Contact verification")
            t0 = datetime.now(timezone.utc)
            contact_result = verify_contact(lead_data, web_result)
            clog.stage("STAGE 3: CONTACT VERIFICATION", contact_result,
                       source=verify_contact, inputs=lead_data, started=t0)
            log.info("  ↳ Stage 3 output: " + _summarise(
                contact_result, ["email_valid", "phone_valid",
                                 "contact_quality_score", "email_notes",
                                 "employment_confirmed"]))
            _save_validation(
                db, lead_id, stage="contact", result=contact_result,
                score=_num(getattr(contact_result, "contact_quality_score", 0)),
                passed=bool(getattr(contact_result, "email_valid", False)
                            or getattr(contact_result, "phone_valid", False)))
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

            t0 = datetime.now(timezone.utc)
            score_result = score_lead(lead_data, icp_result, web_result,
                                      contact_result,
                                      job_id=lead.job_id, lead_id=lead_id)
            clog.stage("SCORING", score_result, source=score_lead, started=t0)

            fs = FinalScore(
                lead_id=         lead_id,
                total_score=     _num(getattr(score_result, "total_score", 0)),
                icp_score=       _num(getattr(score_result, "icp_score", 0)),
                authority_score= _num(getattr(score_result, "authority_score", 0)),
                data_score=      _num(getattr(score_result, "data_score", 0)),
                growth_score=    _num(getattr(score_result, "growth_score", 0)),
                contact_score=   _num(getattr(score_result, "contact_score", 0)),
                priority=        getattr(score_result, "priority", "Low") or "Low",
                reasoning=       getattr(score_result, "reasoning", "") or "",
                growth_signals=  getattr(score_result, "growth_signals", "") or "",
                outreach_intelligence=getattr(score_result, "outreach_intelligence", "") or "",
            )
            db.add(fs)
            db.flush()

            log.info(
                "  ↳ Score breakdown: "
                f"icp={fs.icp_score} authority={fs.authority_score} "
                f"data={fs.data_score} growth={fs.growth_score} "
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
            try:
                raise self.retry(exc=exc)
            except self.MaxRetriesExceededError:
                log.error("Max retries exceeded — marking lead as failed")
                try:
                    clog.note("Max retries exceeded — marking lead as failed")
                    clog.close()
                except Exception:
                    pass
                _mark_lead_finished(db, lead, failed=True)

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

        processed = job.processed_rows or 0
        total = job.total_rows or 0

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
                job.prompt_tokens     = int(row.p or 0)
                job.completion_tokens = int(row.c or 0)
                job.total_tokens_used = int(row.t or 0)
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
        job.status = "completed" if processed > 0 else "failed"
        job.completed_at = datetime.now(timezone.utc)
        db.flush()

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
    db.execute(update(Job).where(Job.id == lead.job_id).values(**values))
    db.flush()


def _get_cached_web(key: str):
    try:
        payload = _redis_cache.get(key)
        if payload:
            data = json.loads(payload)
            result = WebResearchResult(**data)
            result.raw_output = json.dumps(data, default=str)
            return result
    except Exception as exc:
        logger.debug("Redis web cache read unavailable: %s", exc)
    with _web_cache_lock:
        return _web_research_cache.get(key)


def _set_cached_web(key: str, result) -> None:
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
