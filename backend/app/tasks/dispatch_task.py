"""Durable SQL intent and bounded publication for import/qualification jobs.

The page cursor commits only after publication. A crash can replay one page;
per-lead SQL locks and terminal-state guards make that replay idempotent.
"""
import logging
import threading
from functools import lru_cache
from uuid import uuid4
import redis
from datetime import datetime, timedelta, timezone

from celery.signals import worker_ready, worker_shutdown
from sqlalchemy import and_, case, or_

from app.celery_app import celery
from app.config import settings
from app.database import db_session
from app.models import Job, Lead
from app.utils.db_availability import (
    RecoveryBackoff, database_retry_delay, database_unavailable_reason,
    warn_database_unavailable,
)
from app.utils.lead_identity import transaction_lock

logger = logging.getLogger(__name__)
PAGE_SIZE = 250
DISPATCH_INTERVAL = 60
IMPORT_STALE_SECONDS = max(300, int(settings.LLM_TIMEOUT_SECONDS * 2 + 60))
_stop = threading.Event()
_thread = None
BROKER_EPOCH_KEY = "ql:dispatch:broker-epoch:v1"


@lru_cache(maxsize=1)
def _epoch_store():
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3, health_check_interval=30)


def broker_epoch():
    """A missing marker identifies an empty/replaced broker, not a worker restart."""
    client = _epoch_store()
    current = client.get(BROKER_EPOCH_KEY)
    if not current:
        client.set(BROKER_EPOCH_KEY, str(uuid4()), nx=True)
        current = client.get(BROKER_EPOCH_KEY)
    if not current:
        raise RuntimeError("Broker generation could not be established")
    return current


def _ensure_epoch(job, epoch):
    if job.dispatch_epoch == epoch:
        return
    if job.status == "running":
        # A broker restart/data loss may have discarded already published work.
        # Replay only nonterminal rows; completed scores are never researched again.
        job.dispatch_row_index = job.dispatch_lead_id = None
        job.dispatch_complete = False
    job.dispatch_updated_at = None
    job.dispatch_epoch = epoch


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def request_dispatch(job_id: str, *, force=False):
    """Attempt publication without losing committed SQL intent on broker errors."""
    epoch = broker_epoch()
    with db_session() as db:
        transaction_lock(db, "ql:dispatch:" + job_id)
        job = db.get(Job, job_id)
        if job is None or job.status not in ("running", "importing"):
            return
        if job.status == "importing" and not job.source_blob_name:
            return  # Blob transfer has not committed; the uploader can retry safely.
        _ensure_epoch(job, epoch)
        if (job.status == "running" and job.dispatch_complete
                and job.processed_rows < (job.total_rows or 0)):
            return
        interval = IMPORT_STALE_SECONDS if job.status == "importing" else DISPATCH_INTERVAL
        previous = job.dispatch_updated_at
        if previous and previous.tzinfo:
            previous = previous.astimezone(timezone.utc).replace(tzinfo=None)
        if not force and previous and previous > _now() - timedelta(seconds=interval):
            return
        job.dispatch_updated_at = _now()
        try:
            if job.status == "importing":
                from app.tasks.import_task import import_file
                suffix = "." + str(job.file_type or "").lower().lstrip(".")
                if suffix not in (".csv", ".xlsx", ".xls", ".xlsm"):
                    job.status, job.import_error = "import_failed", "The saved upload type is unsupported. Please upload the file again."
                    return
                source = job.source_blob_name
                import_file.apply_async(args=[job_id, source, suffix, job.file_name or f"leads{suffix}"],
                    task_id="import-" + job_id, queue=settings.CELERY_IMPORT_QUEUE, retry=False)
            elif job.dispatch_complete:
                from app.tasks.tasks import finalise_job
                finalise_job.apply_async(args=[[], job_id], task_id="finalise-" + job_id, retry=False)
            else:
                dispatch_job.apply_async(args=[job_id], task_id="dispatch-" + job_id,
                    queue=settings.CELERY_IMPORT_QUEUE, retry=False)
        except Exception:
            # Commit the attempt timestamp to prevent polling stampedes. SQL
            # still says importing/running, and reconciliation will retry.
            logger.exception("Publication unavailable for job %s; SQL intent retained", job_id)


@celery.task(bind=True, name="app.tasks.dispatch_task.dispatch_job", ignore_result=True,
             max_retries=3, queue=settings.CELERY_IMPORT_QUEUE)
def dispatch_job(self, job_id: str):
    from app.tasks.tasks import process_lead
    try:
        epoch = broker_epoch()
        with db_session() as db:
            transaction_lock(db, "ql:dispatch:" + job_id)
            job = db.get(Job, job_id)
            if job is None or job.status != "running":
                return
            _ensure_epoch(job, epoch)
            if job.dispatch_complete:
                return
            query = db.query(Lead.id, Lead.row_index).filter(Lead.job_id == job_id,
                ~Lead.processing_status.in_(("completed", "failed")))
            if job.dispatch_lead_id is not None:
                if job.dispatch_row_index is None:
                    query = query.filter(or_(Lead.row_index.isnot(None),
                        and_(Lead.row_index.is_(None), Lead.id > job.dispatch_lead_id)))
                else:
                    query = query.filter(or_(Lead.row_index > job.dispatch_row_index,
                        and_(Lead.row_index == job.dispatch_row_index, Lead.id > job.dispatch_lead_id)))
            page = query.order_by(Lead.row_index, Lead.id).limit(PAGE_SIZE).all()
            if not page:
                job.dispatch_complete = True
                job.dispatch_updated_at = None
                return
            # Reuse a single producer connection for this bounded page. Global
            # broker socket/connect timeouts bound a failed publish attempt.
            with celery.producer_or_acquire() as producer:
                for lead_id, _ in page:
                    process_lead.apply_async(args=[str(lead_id), job.icp_prompt],
                        task_id="lead-" + str(lead_id), producer=producer, retry=False,
                        headers={"job_id": str(job_id)})
            job.dispatch_row_index, job.dispatch_lead_id = page[-1].row_index, page[-1].id
            job.dispatch_updated_at = _now()
        # Another short task advances the next page. SQL recovery also resumes
        # if this handoff is lost after the cursor commits.
        dispatch_job.apply_async(args=[job_id], task_id="dispatch-" + job_id,
            queue=settings.CELERY_IMPORT_QUEUE, retry=False)
    except Exception as exc:
        if database_unavailable_reason(exc):
            warn_database_unavailable(logger, "Job page dispatch", exc)
            raise self.retry(
                countdown=database_retry_delay(self.request.retries),
                max_retries=self.request.retries + 1,
            )
        logger.exception("Could not publish job page %s", job_id)
        raise self.retry(exc=exc, countdown=15)


def _recover_job_requests():
    now = _now()
    epoch = broker_epoch()
    with db_session() as db:
        ids = [str(row.id) for row in db.query(Job.id).filter(or_(
            and_(Job.status == "importing", Job.source_blob_name.isnot(None),
                or_(Job.dispatch_epoch.is_(None), Job.dispatch_epoch != epoch,
                    Job.dispatch_updated_at.is_(None),
                    Job.dispatch_updated_at <= now - timedelta(seconds=IMPORT_STALE_SECONDS))),
            and_(Job.status == "running", or_(Job.dispatch_epoch.is_(None), Job.dispatch_epoch != epoch,
                and_(or_(Job.dispatch_complete == False, Job.processed_rows >= Job.total_rows),
                    or_(Job.dispatch_updated_at.is_(None),
                        Job.dispatch_updated_at <= now - timedelta(seconds=DISPATCH_INTERVAL)))))
        )).order_by(case((Job.dispatch_updated_at.is_(None), 0), else_=1),
            Job.dispatch_updated_at, Job.created_at, Job.id).limit(100).all()]
    for job_id in ids:
        if _stop.is_set():
            return
        try:
            request_dispatch(job_id)
        except Exception as exc:
            if database_unavailable_reason(exc):
                raise  # Stop the page; the outer recovery loop owns backoff.
            logger.exception("Could not recover committed job %s", job_id)


@worker_ready.connect
def recover_jobs(sender=None, **_kwargs):
    global _thread
    queues = getattr(getattr(sender, "task_consumer", None), "queues", ())
    names = {queue.name for queue in queues}
    recover_imports = settings.CELERY_IMPORT_QUEUE in names
    recover_documents = settings.CELERY_EMBED_QUEUE in names
    if not recover_imports and not recover_documents:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()

    def recover():
        imports = RecoveryBackoff("Job publication recovery", logger)
        documents = RecoveryBackoff("Document ingestion recovery", logger)
        while not _stop.is_set():
            if recover_imports and imports.ready():
                try:
                    _recover_job_requests()
                except Exception as exc:
                    imports.failed(exc)
                else:
                    imports.succeeded()
            if recover_documents and documents.ready():
                try:
                    from app.tasks.embed_task import recover_ingestion_requests
                    recover_ingestion_requests()
                except Exception as exc:
                    documents.failed(exc)
                else:
                    documents.succeeded()
            if _stop.wait(5):
                return
    _thread = threading.Thread(target=recover, name="job-dispatch-recovery", daemon=True)
    _thread.start()

@worker_shutdown.connect
def stop_job_recovery(**_kwargs):
    _stop.set()
