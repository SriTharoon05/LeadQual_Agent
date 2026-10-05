"""Durable, idempotent manual research refresh requests.

Existing qualification results remain available throughout a refresh. Requests
are committed before publishing to Celery, and queued requests can be recovered
by periodic worker reconciliation or the dashboard's status polling.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import db_session, get_db
from app.models import Job, Lead, LeadRefresh
from app.utils.lead_identity import transaction_lock

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Evidence refresh"])
ACTIVE_STATUSES = ("queued", "running")
DISPATCH_INTERVAL_SECONDS = 60
STALE_RUNNING_SECONDS = max(900, int(settings.LLM_TIMEOUT_SECONDS * 8 + 120))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class RefreshRequest(BaseModel):
    request_id: UUID


class RefreshRun(BaseModel):
    id: str
    lead_id: str
    status: Literal["queued", "running", "completed", "failed"]
    requested_by: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class RefreshStatus(BaseModel):
    active: RefreshRun | None = None
    latest: RefreshRun | None = None


def refresh_run(run: LeadRefresh) -> RefreshRun:
    return RefreshRun(
        id=str(run.id), lead_id=str(run.lead_id), status=run.status,
        requested_by=run.requested_by, created_at=_utc(run.created_at),
        started_at=_utc(run.started_at), finished_at=_utc(run.finished_at),
        error_message=run.error_message,
    )


def _lead(db: Session, job_id: str, lead_id: str) -> tuple[Job, Lead]:
    job = db.get(Job, job_id)
    lead = db.get(Lead, lead_id)
    if job is None or lead is None or str(lead.job_id) != job_id:
        raise HTTPException(status_code=404, detail="Lead not found in this upload.")
    return job, lead


def dispatch_refresh(request_id: str, *, force: bool = False) -> None:
    """Publish a committed request, with a bounded redispatch interval.

    Publishing and acknowledgement cannot share one transaction with SQL. An
    occasional duplicate is therefore intentional: the worker's identity lock
    and terminal-state guard make redelivery safe. Broker failures leave the
    SQL outbox queued so a retry does not silently lose the user's request.
    """
    from app.tasks.refresh_task import refresh_lead

    with db_session() as db:
        transaction_lock(db, "ql:refresh-dispatch:" + request_id)
        run = db.get(LeadRefresh, request_id)
        if run is None or run.status not in ACTIVE_STATUSES:
            return
        now = _now()
        if run.status == "running":
            started = run.started_at
            if started is not None and started.tzinfo is not None:
                started = started.astimezone(timezone.utc).replace(tzinfo=None)
            if started is not None and started > now - timedelta(seconds=STALE_RUNNING_SECONDS):
                return
        previous = run.last_dispatched_at
        if previous is not None and previous.tzinfo is not None:
            previous = previous.astimezone(timezone.utc).replace(tzinfo=None)
        if not force and previous is not None and previous > now - timedelta(
                seconds=DISPATCH_INTERVAL_SECONDS):
            return
        # Record this attempt even when the broker is unavailable, so dashboard
        # polling cannot repeatedly hammer a failed broker.
        run.last_dispatched_at = now
        try:
            refresh_lead.apply_async(
                args=[str(run.id), str(run.lead_id)],
                task_id="evidence-refresh-" + str(run.id), retry=False,
            )
            if run.status == "queued":
                run.error_message = None
        except Exception:
            logger.exception("Could not publish evidence refresh %s", request_id)
            run.error_message = (
                "The processing service is unavailable. Your previous result is unchanged; "
                "the refresh request will be retried automatically."
            )


@router.post("/jobs/{job_id}/results/{lead_id}/refresh", response_model=RefreshRun,
             status_code=202)
def request_refresh(job_id: UUID, lead_id: UUID, body: RefreshRequest,
                    request: Request, db: Session = Depends(get_db)):
    job_key, lead_key, request_key = str(job_id), str(lead_id), str(body.request_id)
    try:
        # Job dispatch/deletion use this short transaction lock, so a refresh
        # cannot be inserted after deletion's active-request check.
        transaction_lock(db, "ql:dispatch:" + job_key)
        transaction_lock(db, "ql:refresh-request:" + lead_key)
        job, lead = _lead(db, job_key, lead_key)
        run = db.get(LeadRefresh, request_key)
        if run is not None and str(run.lead_id) != lead_key:
            raise HTTPException(status_code=409,
                                detail="This refresh request belongs to another lead.")
        if run is not None and run.status in ("running", "completed"):
            db.commit()
            if run.status == "running":
                dispatch_refresh(request_key)
                db.refresh(run)
            return refresh_run(run)
        if job.status not in ("completed", "failed") or lead.processing_status not in (
                "completed", "failed"):
            raise HTTPException(status_code=409,
                                detail="Wait for this upload to finish before refreshing evidence.")
        active = (db.query(LeadRefresh).filter(
            LeadRefresh.lead_id == lead_key, LeadRefresh.status.in_(ACTIVE_STATUSES)
        ).first())
        if active is not None and str(active.id) != request_key:
            raise HTTPException(status_code=409,
                                detail="An evidence refresh is already active for this lead.")
        if run is None:
            run = LeadRefresh(
                id=request_key, lead_id=lead_key, status="queued",
                requested_by=str(getattr(request.state, "username", "") or "")[:100] or None,
                created_at=_now(), attempt_count=0,
            )
            db.add(run)
        elif run.status == "failed":
            newer = (db.query(LeadRefresh.id).filter(
                LeadRefresh.lead_id == lead_key, LeadRefresh.id != request_key,
                LeadRefresh.created_at > run.created_at).first())
            if newer is not None:
                raise HTTPException(status_code=409,
                                    detail="Start a new refresh request for the current result.")
            # A manual retry starts a new bounded attempt budget while retaining
            # the original snapshot and request identity for auditing.
            run.status = "queued"
            run.attempt_count = 0
            run.started_at = None
            run.finished_at = None
            run.error_message = None
            run.last_dispatched_at = None
        db.commit()
        dispatch_refresh(request_key, force=True)
        db.refresh(run)
        return refresh_run(run)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not request evidence refresh for lead %s", lead_key)
        raise HTTPException(status_code=503, detail="The lead database is temporarily unavailable.",
                            headers={"Retry-After": "3"})
    except RuntimeError:
        db.rollback()
        logger.exception("Could not lock evidence refresh request for lead %s", lead_key)
        raise HTTPException(status_code=503, detail="The lead is busy. Please retry this request.",
                            headers={"Retry-After": "3"})


@router.get("/jobs/{job_id}/results/{lead_id}/refresh", response_model=RefreshStatus)
def get_refresh_status(job_id: UUID, lead_id: UUID, db: Session = Depends(get_db)):
    try:
        _lead(db, str(job_id), str(lead_id))
        latest = (db.query(LeadRefresh).filter(LeadRefresh.lead_id == str(lead_id))
                  .order_by(LeadRefresh.created_at.desc(), LeadRefresh.id.desc()).first())
        active = (db.query(LeadRefresh).filter(
            LeadRefresh.lead_id == str(lead_id), LeadRefresh.status.in_(ACTIVE_STATUSES)
        ).order_by(LeadRefresh.created_at.desc(), LeadRefresh.id.desc()).first())
        if active is not None:
            request_key = str(active.id)
            db.commit()
            dispatch_refresh(request_key)
            db.refresh(active)
            if latest is not None and str(latest.id) != request_key:
                db.refresh(latest)
            if active.status not in ACTIVE_STATUSES:
                latest = active
                active = None
        return RefreshStatus(active=refresh_run(active) if active else None,
                             latest=refresh_run(latest) if latest else None)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not load refresh status for lead %s", lead_id)
        raise HTTPException(status_code=503, detail="The lead database is temporarily unavailable.",
                            headers={"Retry-After": "3"})
    except RuntimeError:
        db.rollback()
        logger.exception("Could not reconcile evidence refresh for lead %s", lead_id)
        raise HTTPException(status_code=503, detail="Refresh status is temporarily unavailable.",
                            headers={"Retry-After": "3"})
