"""Durable, uncached pause checks for background work and paid requests.

Never use a worker's ORM identity map or a Redis cache as the billing gate.
A failed SQL check propagates: an unavailable status cannot authorize a charge.
"""
from app.database import SessionLocal
from app.models import Job, Lead


class JobPaused(Exception):
    """Acknowledge this delivery; durable SQL intent resumes after unpause."""


def assert_job_not_paused(job_id=None, *, lead_id=None):
    if not job_id and not lead_id:
        return
    with SessionLocal() as db:
        if not job_id:
            job_id = db.query(Lead.job_id).filter(Lead.id == lead_id).scalar()
            if not job_id:
                raise JobPaused("Lead no longer exists")
        state = db.query(Job.status).filter(Job.id == job_id).scalar()
    if state is None or state == "paused":
        raise JobPaused("Job is paused or no longer exists")


def background_allowed(db, job_id):
    """Fresh scalar read, including when a long-lived session has a cached Job."""
    state = db.query(Job.status).filter(Job.id == job_id).scalar()
    return state is not None and state != "paused"
