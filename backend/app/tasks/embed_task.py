"""Durable asynchronous website ingestion and complete, retry-safe embeddings.

Qualification only records SQL intent. Scraping, Blob archival, embedding and
Qdrant writes run on lead_embeddings and cannot occupy qualification slots.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, case, exists, or_
from sqlalchemy.orm import aliased, load_only, noload

from app.celery_app import celery
from app.config import settings
from app.database import SessionLocal, db_session, locked_lead_session
from app.models import CompanyScrape, Lead
from app.scraper.web_scraper import research_company
from app.utils.db_availability import database_unavailable_reason
from app.utils.lead_identity import transaction_lock
from app.vectorstore.embedder import chunk_text, embed
from app.vectorstore.qdrant_store import upsert_chunks

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
DISPATCH_SECONDS = 60
STALE_SECONDS = max(300, int(settings.LLM_TIMEOUT_SECONDS * 3))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _s(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ", ".join(_s(item) for item in value if item is not None)
    return str(value).strip()


def _latest_query(db, lead_id):
    return db.query(CompanyScrape).filter(CompanyScrape.lead_id == lead_id).order_by(
        CompanyScrape.scraped_at.desc(), CompanyScrape.id.desc())


def persist_and_dispatch_embed(db, lead, web_result=None, clog=None, *, force=False):
    """Commit ingestion intent without doing website/embedding work inline.

    Existing successful text and its observation date remain available while
    refreshed content is queued. Historical duplicate rows are not deleted.
    """
    if lead is None:
        return
    lead_id, job_id = str(lead.id), str(lead.job_id)
    scrape_id = None
    try:
        with db_session() as ingestion:
            transaction_lock(ingestion, "ql:ingest:" + lead_id)
            row = _latest_query(ingestion, lead_id).first()
            if row is None:
                row = CompanyScrape(job_id=job_id, lead_id=lead_id,
                    company_name=_s(lead.company_name), website_url=_s(lead.company_website),
                    ingest_status="queued", ingest_attempts=0, embedded=0)
                ingestion.add(row)
            elif not force and row.ingest_status in ("completed", "failed"):
                return
            elif force:
                row.ingest_status = "queued"
                row.ingest_attempts = 0
                row.ingest_dispatched_at = None
                row.ingest_error = None
            ingestion.flush()
            scrape_id = str(row.id)
        dispatch_ingestion(scrape_id, force=True)
    except Exception:
        logger.exception("Website ingestion queued/recoverable failure for lead %s", lead_id)


def dispatch_ingestion(scrape_id, *, force=False):
    with db_session() as db:
        transaction_lock(db, "ql:ingest-dispatch:" + scrape_id)
        row = db.query(CompanyScrape).options(load_only(
            CompanyScrape.id, CompanyScrape.lead_id, CompanyScrape.ingest_status,
            CompanyScrape.ingest_attempts, CompanyScrape.ingest_dispatched_at,
            CompanyScrape.ingest_error)).filter(CompanyScrape.id == scrape_id).first()
        if row is None or row.ingest_status in ("completed", "failed"):
            return
        previous = row.ingest_dispatched_at
        if previous and previous.tzinfo:
            previous = previous.astimezone(timezone.utc).replace(tzinfo=None)
        interval = STALE_SECONDS if row.ingest_status == "scraping" else DISPATCH_SECONDS
        if not force and previous and previous > _now() - timedelta(seconds=interval):
            return
        row.ingest_dispatched_at = _now()
        try:
            task = embed_company if row.ingest_status == "embedding" else ingest_company
            task.apply_async(args=[scrape_id], task_id="ingestion-" + scrape_id,
                queue=settings.CELERY_EMBED_QUEUE, retry=False)
        except Exception:
            row.ingest_error = "Indexing service unavailable; the saved request will retry automatically."
            logger.exception("Could not publish ingestion %s", scrape_id)


def recover_ingestion_requests():
    """Recover pending newest documents without replaying historical duplicates."""
    now = _now()
    with db_session() as db:
        newer = aliased(CompanyScrape)
        rows = db.query(CompanyScrape.id).filter(
            CompanyScrape.ingest_status.in_(("queued", "scraping", "embedding")),
            ~exists().where(newer.lead_id == CompanyScrape.lead_id, or_(
                newer.scraped_at > CompanyScrape.scraped_at,
                and_(newer.scraped_at == CompanyScrape.scraped_at, newer.id > CompanyScrape.id))),
            or_(CompanyScrape.ingest_dispatched_at.is_(None),
                and_(CompanyScrape.ingest_status == "scraping",
                    CompanyScrape.ingest_dispatched_at <= now - timedelta(seconds=STALE_SECONDS)),
                and_(CompanyScrape.ingest_status != "scraping",
                    CompanyScrape.ingest_dispatched_at <= now - timedelta(seconds=DISPATCH_SECONDS)))
        ).order_by(case((CompanyScrape.ingest_dispatched_at.is_(None), 0), else_=1),
                   CompanyScrape.ingest_dispatched_at, CompanyScrape.id).limit(50).all()
    for row in rows:
        try:
            dispatch_ingestion(str(row.id))
        except Exception as exc:
            if database_unavailable_reason(exc):
                raise  # One outage must not trigger another 49 failed writes.
            logger.exception("Could not recover document ingestion %s", row.id)


def _claim(db, row, phase):
    if row.ingest_status in ("completed", "failed"):
        return False
    latest = _latest_query(db, row.lead_id).with_entities(CompanyScrape.id).first()
    if latest is None or str(latest.id) != str(row.id):
        return False
    if int(row.ingest_attempts or 0) >= MAX_ATTEMPTS:
        row.ingest_status = "failed"
        row.ingest_error = "Indexing could not finish after several attempts. Saved SQL details remain available; refresh evidence to retry."
        db.commit()
        return False
    row.ingest_attempts = int(row.ingest_attempts or 0) + 1
    row.ingest_status = phase
    row.ingest_dispatched_at = _now()
    row.ingest_error = None
    db.commit()
    return True


def _failure(db, scrape_id, phase, exc):
    db.rollback()
    row = db.get(CompanyScrape, scrape_id)
    if row is not None and row.ingest_status not in ("completed", "failed"):
        terminal = int(row.ingest_attempts or 0) >= MAX_ATTEMPTS
        row.ingest_status = "failed" if terminal else phase
        row.ingest_error = ("Website indexing could not complete. Saved SQL details remain available. "
                            + ("Refresh evidence to retry." if terminal else "Another attempt is scheduled."))
        row.ingest_dispatched_at = _now()
        db.commit()
    logger.warning("Document ingestion %s failed (%s)", scrape_id, type(exc).__name__)


def _lead_for_scrape(scrape_id):
    with SessionLocal() as db:
        value = db.query(CompanyScrape.lead_id).filter(CompanyScrape.id == scrape_id).first()
        return str(value.lead_id) if value else None


@celery.task(bind=True, name="app.tasks.embed_task.ingest_company", max_retries=5,
             default_retry_delay=30, acks_late=True, queue=settings.CELERY_EMBED_QUEUE)
def ingest_company(self, scrape_id):
    lead_id = _lead_for_scrape(scrape_id)
    if not lead_id:
        return
    try:
        with locked_lead_session(lead_id, lock_timeout_ms=1000, artifact_shared=True) as db:
            row = db.get(CompanyScrape, scrape_id)
            lead = db.query(Lead).options(noload(Lead.refresh_requests)).filter(Lead.id == lead_id).first()
            if row is None or lead is None or row.ingest_status == "embedding":
                return
            if not _claim(db, row, "scraping"):
                return
            company, website = _s(lead.company_name), _s(lead.company_website)
            description, technologies = _s(lead.company_description), _s(lead.technologies_used)
            job_id = str(lead.job_id)
            from app.admin.telemetry import set_context
            set_context(job_id=job_id, lead_id=lead_id, stage="website_ingestion")
            db.commit()
            try:
                research = research_company(company, website)
                if not isinstance(research, dict) or not _s(research.get("text")):
                    raise ValueError("No readable website content was returned")
                text = _s(research.get("text"))
                limit = settings.SCRAPE_MAX_TEXT_CHARS
                if len(text) > limit:
                    text = text[:limit] + "\n[Website text reached the configured size limit.]"
                clean_text = "\n\n".join(part for part in (text, description, technologies) if part)
                source_url = _s(research.get("source_url")) or website
                blob_path = _archive_raw_to_blob(job_id, lead_id, research)
                db.expire_all()
                row = db.get(CompanyScrape, scrape_id)
                if row is None:
                    return
                row.company_name, row.website_url = company, source_url[:500]
                row.clean_text, row.blob_path = clean_text, blob_path
                row.scraped_at = _now()
                row.embedded, row.chunk_count = 0, 0
                row.ingest_status, row.ingest_dispatched_at, row.ingest_error = "embedding", None, None
                db.commit()
            except Exception as exc:
                _failure(db, scrape_id, "queued", exc)
                raise
        dispatch_ingestion(scrape_id, force=True)
    except TimeoutError as exc:
        raise self.retry(exc=exc, countdown=10)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30)


@celery.task(bind=True, name="app.tasks.embed_task.embed_company", max_retries=5,
             default_retry_delay=30, acks_late=True, queue=settings.CELERY_EMBED_QUEUE)
def embed_company(self, scrape_id):
    lead_id = _lead_for_scrape(scrape_id)
    if not lead_id:
        return
    try:
        with locked_lead_session(lead_id, lock_timeout_ms=1000, artifact_shared=True) as db:
            row = db.get(CompanyScrape, scrape_id)
            if row is None or row.ingest_status != "embedding":
                return
            if db.query(Lead.id).filter(Lead.id == lead_id).first() is None:
                return
            if row.embedded:
                row.ingest_status = "completed"
                db.commit()
                return
            if not _claim(db, row, "embedding"):
                return
            text, company = _s(row.clean_text), _s(row.company_name) or "Unknown"
            job_id, website, observed_at = str(row.job_id), _s(row.website_url), row.scraped_at
            from app.admin.telemetry import set_context
            set_context(job_id=job_id, lead_id=lead_id, stage="embedding")
            db.commit()  # No database transaction remains open across network waits.
            try:
                chunks = chunk_text(text)
                if not chunks:
                    raise ValueError("The saved document has no indexable text")
                vectors = embed(chunks, job_id=job_id, lead_id=lead_id)
                if len(vectors) != len(chunks):
                    raise ValueError("Embedding response did not match the complete document")
                written = upsert_chunks(job_id=job_id, lead_id=lead_id, company_name=company,
                    chunks=chunks, vectors=vectors, website_url=website)
                if written != len(chunks):
                    raise ValueError("The complete document was not indexed")
                db.expire_all()
                row = db.get(CompanyScrape, scrape_id)
                if row is None or row.scraped_at != observed_at or _s(row.clean_text) != text:
                    return
                row.embedded, row.chunk_count = 1, written
                row.ingest_status, row.ingest_error = "completed", None
                db.commit()
            except Exception as exc:
                _failure(db, scrape_id, "embedding", exc)
                raise
    except TimeoutError as exc:
        raise self.retry(exc=exc, countdown=10)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30)


def _archive_raw_to_blob(job_id, lead_id, research):
    try:
        from azure.storage.blob import BlobServiceClient
        with BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING,
                connection_timeout=10, read_timeout=60, retry_total=2) as client:
            blob_name = f"scraped/{job_id}/{lead_id}/raw_{uuid4().hex}.json"
            client.get_blob_client(settings.AZURE_BLOB_CONTAINER, blob_name).upload_blob(
                json.dumps(research, ensure_ascii=False, default=str).encode("utf-8"), overwrite=True)
            return blob_name
    except Exception:
        logger.warning("Could not archive website content for lead %s", lead_id)
        return ""
