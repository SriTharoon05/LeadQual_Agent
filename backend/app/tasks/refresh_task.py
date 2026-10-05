"""Fresh research with atomic result replacement and crash-safe redelivery."""

import hashlib
import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone

from celery.signals import worker_ready, worker_shutdown
from sqlalchemy import and_, case, or_

from app.celery_app import celery
from app.database import db_session, locked_lead_session
from app.models import CompanyScrape, FinalScore, Lead, LeadRefresh, ValidationResult
from app.scoring.scoring_engine import score_lead
from app.tasks.tasks import (
    _lead_to_dict, _num, _save_validation, _set_cached_web, _web_score,
)
from app.utils.lead_identity import json_object
from app.validators.contact_verifier import verify_contact
from app.validators.icp_validator import validate_icp
from app.validators.web_researcher import research_lead

logger = logging.getLogger(__name__)
MAX_RESEARCH_ATTEMPTS = 4
_recovery_stop = threading.Event()
_recovery_thread: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RefreshStageError(RuntimeError):
    """A recoverable stage failed; the original result must stay untouched."""


def _require_result(result, stage: str) -> None:
    data = json_object(getattr(result, "raw_output", None))
    if (result is None or not data or getattr(result, "error", None)
            or data.get("error") or data.get("fallback")):
        raise RefreshStageError(f"{stage} did not produce a successful result")
    if stage == "Web research" and not data.get("evidence"):
        raise RefreshStageError("Web research did not return structured evidence")
    if stage == "ICP validation" and not data.get("checks"):
        raise RefreshStageError("ICP validation did not return structured checks")
    if stage == "Scoring":
        for field in ("icp_score", "authority_score", "data_score", "growth_score", "contact_score"):
            try:
                value = data[field]
                if isinstance(value, bool) or not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
                    raise ValueError()
            except (KeyError, TypeError, ValueError, OverflowError):
                raise RefreshStageError("Scoring returned incomplete or invalid component scores")


def _row_snapshot(row) -> dict | None:
    if row is None:
        return None
    return {column.name: getattr(row, column.name)
            for column in row.__table__.columns}


def _snapshot(db, lead: Lead) -> str:
    """Retain the previous qualification evidence without changing other jobs."""
    payload = {
        "lead_id": str(lead.id), "captured_at": _now().isoformat() + "Z",
        "company_description": lead.company_description,
        "technologies_used": lead.technologies_used,
        "evidence_checked_at": lead.evidence_checked_at,
        "reused_from_lead_id": lead.reused_from_lead_id,
        "reuse_kind": lead.reuse_kind,
        "validation_results": [_row_snapshot(row) for row in lead.validation_results],
        "final_score": _row_snapshot(lead.final_score),
        "company_scrapes": [_row_snapshot(row) for row in db.query(CompanyScrape)
                            .filter(CompanyScrape.lead_id == lead.id).all()],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


@celery.task(
    name="app.tasks.refresh_task.refresh_lead", bind=True,
    max_retries=12, default_retry_delay=30, acks_late=True,
    reject_on_worker_lost=True,
)
def refresh_lead(self, request_id: str, lead_id: str):
    """Serialize by shared identity, including normal upload workers.

    SQL session locks survive commits, but no SQL transaction is held open
    while researching. A crashed worker releases the lock and redelivery can
    resume its running request. The persisted budget bounds repeated paid work.
    """
    try:
        with locked_lead_session(lead_id) as db:
            return _refresh(db, request_id, lead_id)
    except TimeoutError as exc:
        # Contention consumes transport retries, not research attempts. Status
        # reconciliation can redispatch if the bounded retries are exhausted.
        raise self.retry(exc=exc, countdown=30)
    except Exception as exc:
        logger.exception("Evidence refresh task %s failed", request_id)
        raise self.retry(exc=exc, countdown=min(300, 30 * (self.request.retries + 1)))


def _refresh(db, request_id: str, lead_id: str):
    run = db.get(LeadRefresh, request_id)
    lead = db.get(Lead, lead_id)
    if run is None or lead is None or str(run.lead_id) != lead_id:
        return
    if run.status in ("completed", "failed"):
        return
    if lead.processing_status not in ("completed", "failed"):
        run.status = "failed"
        run.error_message = "The original upload has not finished processing."
        run.finished_at = _now()
        return
    attempts = int(run.attempt_count or 0)
    if attempts >= MAX_RESEARCH_ATTEMPTS:
        run.status = "failed"
        run.error_message = "Evidence refresh could not finish after several attempts. Your previous result is unchanged."
        run.finished_at = _now()
        return
    job = lead.job
    if job is None:
        return
    lead_data = _lead_to_dict(lead)
    icp_prompt, job_id = job.icp_prompt, str(job.id)
    if run.previous_snapshot_json is None:
        run.previous_snapshot_json = _snapshot(db, lead)
    run.status = "running"
    run.attempt_count = attempts + 1
    run.started_at = _now()
    run.finished_at = None
    run.error_message = None
    db.commit()

    try:
        # Intentionally bypass both saved stage rows and the identity web cache.
        web = research_lead(lead_data, job_id=job_id, lead_id=lead_id)
        _require_result(web, "Web research")
        contact = verify_contact(lead_data, web)
        _require_result(contact, "Contact verification")
        email_notes = str(getattr(contact, "email_notes", ""))
        # The contact verifier intentionally records DNS failures as notes.
        # NXDOMAIN/NoAnswer are negative findings; resolver/network timeouts are
        # failed checks and must not replace a previously qualified contact.
        dns_failure = (email_notes.startswith("No MX record for ")
                       and email_notes.rsplit(": ", 1)[-1] not in {"NXDOMAIN", "NoAnswer"})
        if email_notes.startswith("Email check error:") or dns_failure:
            raise RefreshStageError("Contact verification could not finish")
        icp = validate_icp(lead_data, icp_prompt, job_id=job_id, lead_id=lead_id,
                           evidence=web.evidence, contact_result=contact)
        _require_result(icp, "ICP validation")
        lead_data["Company Description"] = web.company_description or ""
        lead_data["Technologies Used"] = web.technologies_used or ""
        score = score_lead(lead_data, icp, web, contact, job_id=job_id, lead_id=lead_id)
        _require_result(score, "Scoring")

        # Refresh loaded objects after the external waits; deletion or other
        # committed changes must not be silently overwritten by stale objects.
        db.expire_all()
        run, lead = db.get(LeadRefresh, request_id), db.get(Lead, lead_id)
        if run is None or lead is None or run.status != "running":
            return
        _save_validation(db, lead_id, "web", web, _web_score(web),
                         bool(web.company_verified), sources=web.sources)
        _save_validation(db, lead_id, "contact", contact,
                         _num(contact.contact_quality_score),
                         bool(contact.email_valid or contact.phone_valid))
        _save_validation(db, lead_id, "icp", icp, _num(icp.icp_score), bool(icp.icp_match))
        # Preserve score identity so consumers always see one current score.
        final = lead.final_score
        if final is None:
            final = FinalScore(lead_id=lead_id)
            db.add(final)
        for field in ("total_score", "icp_score", "authority_score", "data_score",
                      "growth_score", "contact_score"):
            setattr(final, field, _num(getattr(score, field, 0)))
        for field in ("priority", "reasoning", "growth_signals", "outreach_intelligence"):
            setattr(final, field, getattr(score, field, "") or "")
        final.created_at = _now()
        lead.company_description = web.company_description or ""
        lead.technologies_used = web.technologies_used or ""
        # The researcher's checked_at is produced only after a successful search.
        lead.evidence_checked_at = (
            datetime.fromisoformat(web.checked_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc).replace(tzinfo=None)
        )
        db.query(ValidationResult).filter(ValidationResult.lead_id == lead_id).update(
            {ValidationResult.created_at: lead.evidence_checked_at}, synchronize_session=False)
        lead.reused_from_lead_id = None
        lead.reuse_kind = None
        # Older scrape prose can contradict refreshed employment/company facts.
        # Proof-backed fresh descriptions replace it in the chatbot; embedding
        # availability is deliberately outside the refresh's critical path.
        db.query(CompanyScrape).filter(CompanyScrape.lead_id == lead_id).delete(
            synchronize_session=False)
        run.status = "completed"
        run.finished_at = _now()
        run.error_message = None
        # All three validation stages, score, prose and request state commit
        # together. Manual tracking and historical upload counters stay intact.
        db.commit()
        identity = lead.research_fingerprint or lead.id
        cache_key = "ql:web:v2:" + hashlib.sha256(str(identity).encode("utf-8")).hexdigest()
        _set_cached_web(cache_key, web)
        return
    except Exception:
        db.rollback()
        run = db.get(LeadRefresh, request_id)
        if run is not None and run.status not in ("completed", "failed"):
            terminal = int(run.attempt_count or 0) >= MAX_RESEARCH_ATTEMPTS
            run.status = "failed" if terminal else "queued"
            run.error_message = (
                "Evidence refresh could not finish. Your previous result is unchanged. "
                + ("You can retry the refresh." if terminal else "Another attempt is scheduled.")
            )
            run.finished_at = _now() if terminal else None
            run.last_dispatched_at = _now()
            db.commit()
        raise


@worker_ready.connect
def recover_refresh_requests(**_kwargs):
    """Periodically reconcile durable requests without requiring Celery Beat.

    Work happens outside the worker-ready signal thread. Rotating bounded scans
    eventually reach larger queues, including while the dashboard is closed.
    Each publish is still protected by its SQL dispatch lock.
    """
    global _recovery_thread
    if _recovery_thread is not None and _recovery_thread.is_alive():
        return
    _recovery_stop.clear()

    def recover():
        from app.refresh import (
            DISPATCH_INTERVAL_SECONDS, STALE_RUNNING_SECONDS, dispatch_refresh,
        )
        while not _recovery_stop.is_set():
            try:
                now = _now()
                dispatch_due = or_(LeadRefresh.last_dispatched_at.is_(None),
                                   LeadRefresh.last_dispatched_at <= now - timedelta(
                                       seconds=DISPATCH_INTERVAL_SECONDS))
                abandoned_running = and_(
                    LeadRefresh.status == "running",
                    or_(LeadRefresh.started_at.is_(None),
                        LeadRefresh.started_at <= now - timedelta(seconds=STALE_RUNNING_SECONDS)),
                )
                with db_session() as db:
                    ids = [str(row.id) for row in db.query(LeadRefresh.id)
                           .filter(dispatch_due, or_(LeadRefresh.status == "queued", abandoned_running))
                           .order_by(
                               case((LeadRefresh.last_dispatched_at.is_(None), 0), else_=1).asc(),
                               LeadRefresh.last_dispatched_at.asc(), LeadRefresh.created_at.asc(),
                               LeadRefresh.id.asc(),
                           ).limit(100).all()]
                for request_id in ids:
                    if _recovery_stop.is_set():
                        break
                    try:
                        dispatch_refresh(request_id)
                    except Exception:
                        logger.exception("Could not recover refresh request %s", request_id)
            except Exception:
                logger.exception("Refresh reconciliation scan failed")
            if _recovery_stop.wait(DISPATCH_INTERVAL_SECONDS):
                break

    _recovery_thread = threading.Thread(target=recover, name="refresh-reconciliation", daemon=True)
    _recovery_thread.start()


@worker_shutdown.connect
def stop_refresh_reconciliation(**_kwargs):
    _recovery_stop.set()
