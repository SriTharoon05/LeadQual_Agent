"""
tasks/embed_task.py
───────────────────
Async ingestion of scraped website text into Qdrant. (HARDENED edition)

Flow:
  process_lead (after enrichment)
      → persist_and_dispatch_embed(db, lead, web_result, clog=clog)
            → research_company(...)            # SCRAPE + PARSE
            → archive raw text to Blob          (Layer A)
            → save clean text to CompanyScrape  (Layer B, SQL)
            → embed_company.delay(scrape.id)    # async
                  → chunk → embed → upsert to Qdrant (Layer C)

Design principle: RAG ingestion is a BEST-EFFORT side-feature. It must NEVER
crash the main lead pipeline, and its own async task must fail gracefully
(mark the row done, don't retry forever) rather than hang the queue.
"""

import logging
from datetime import datetime

from app.celery_app import celery
from app.database import db_session
from app.scraper.web_scraper import research_company
from app.utils.company_logger import IST
from app.vectorstore.embedder import chunk_text, embed
from app.vectorstore.qdrant_store import upsert_chunks
from app.models import CompanyScrape

logger = logging.getLogger(__name__)

MAX_NOT_FOUND_RETRIES = 5   # give the parent txn time to commit, then give up


# ── Safe helpers ──────────────────────────────────────────────────────────────
def _s(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ", ".join(_s(v) for v in value if v is not None).strip()
    try:
        return str(value).strip()
    except Exception:
        return default


# ── Task: embed one company's scraped text ────────────────────────────────────
@celery.task(
    name="app.tasks.embed_task.embed_company",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
    acks_late=True,
)
def embed_company(self, scrape_id: str):
    """Chunk → embed → upsert a single company's scraped text into Qdrant."""
    with db_session() as db:
        try:
            scrape = db.query(CompanyScrape).filter(CompanyScrape.id == scrape_id).first()
        except Exception as e:
            logger.error(f"embed_company: DB load failed for {scrape_id}: {e}")
            raise self.retry(exc=e, countdown=15)

        if not scrape:
            # Parent process_lead txn may not have committed yet — retry a few
            # times, then give up (don't retry forever on a genuinely missing row).
            if self.request.retries >= MAX_NOT_FOUND_RETRIES:
                logger.error(
                    f"CompanyScrape {scrape_id} still not found after "
                    f"{MAX_NOT_FOUND_RETRIES} retries — giving up"
                )
                return
            logger.warning(
                f"CompanyScrape {scrape_id} not found yet "
                f"(retry {self.request.retries + 1}/{MAX_NOT_FOUND_RETRIES})"
            )
            raise self.retry(countdown=30)

        company = _s(scrape.company_name) or "Unknown"

        # ── Chunk (guarded) ──────────────────────────────────────────────────
        try:
            chunks = chunk_text(_s(scrape.clean_text)) or []
        except Exception as e:
            logger.error(f"chunk_text failed for '{company}' ({scrape_id}): {e}")
            chunks = []

        if not chunks:
            logger.info(f"No text to embed for '{company}'")
            _mark_done(db, scrape, count=0)
            return

        # ── Embed (guarded — retry, since API blips are transient) ───────────
        try:
            vectors = embed(chunks) or []
        except Exception as exc:
            logger.error(f"embed() failed for '{company}' ({scrape_id}): {exc}")
            raise self.retry(exc=exc)

        # Guard: vectors must align 1:1 with chunks, else Qdrant upsert corrupts.
        if len(vectors) != len(chunks):
            logger.warning(
                f"embed count mismatch for '{company}': "
                f"{len(vectors)} vectors vs {len(chunks)} chunks — truncating to min"
            )
            n = min(len(vectors), len(chunks))
            chunks, vectors = chunks[:n], vectors[:n]

        if not vectors:
            logger.warning(f"No vectors produced for '{company}' — marking done (0)")
            _mark_done(db, scrape, count=0)
            return

        # ── Upsert (guarded — retry on transient Qdrant issues) ──────────────
        try:
            written = upsert_chunks(
                job_id=scrape.job_id,
                lead_id=scrape.lead_id,
                company_name=company,
                chunks=chunks,
                vectors=vectors,
                website_url=_s(scrape.website_url),
            )
        except Exception as exc:
            logger.error(f"upsert_chunks failed for '{company}' ({scrape_id}): {exc}")
            raise self.retry(exc=exc)

        _mark_done(db, scrape, count=written)
        logger.info(f"Embedded '{company}' (job={scrape.job_id}): {written} chunks")


def _mark_done(db, scrape, count: int) -> None:
    """Flag the scrape row as embedded. Never raises hard."""
    try:
        scrape.chunk_count = int(count or 0)
        scrape.embedded = 1
        db.flush()
    except Exception as e:
        logger.warning(f"_mark_done failed for {getattr(scrape, 'id', '?')}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Called from process_lead (after AI enrichment). Best-effort — never breaks
# the main pipeline.
#     persist_and_dispatch_embed(db, lead, web_result, clog=clog)
# ──────────────────────────────────────────────────────────────────────────────
def persist_and_dispatch_embed(db, lead, web_result=None, clog=None) -> None:
    """
    SCRAPE + PARSE + STORE(A/B) + dispatch embedding for one company.
    Enriched fields (already on `lead`) are appended for richer chunks.
    """
    try:
        if lead is None:
            logger.warning("persist_and_dispatch_embed: lead is None — skipping")
            return

        company = _s(getattr(lead, "company_name", "")) or "Unknown"
        website = _s(getattr(lead, "company_website", ""))
        t_scrape = datetime.now(IST)

        # ── 1️⃣ + 2️⃣  SCRAPE + PARSE (guarded) ──────────────────────────────
        try:
            research = research_company(company, website)
        except Exception as e:
            logger.warning(f"research_company failed for '{company}': {e}")
            research = {}
        if not isinstance(research, dict):
            research = {}

        scraped_text = _s(research.get("text"))
        source_url = _s(research.get("source_url")) or website

        # ── Merge enriched fields for richer chunks ──────────────────────────
        clean_text = "\n\n".join(b for b in [
            scraped_text,
            _s(getattr(lead, "company_description", "")),
            _s(getattr(lead, "technologies_used", "")),
        ] if b).strip()

        if not clean_text:
            logger.info(f"persist_and_dispatch_embed: no text for '{company}'")
            _safe_clog_scrape(clog, research, clean_text="", blob_path="", started=t_scrape)
            return

        # ── 3️⃣  STORE Layer A: raw scrape → Blob (best-effort) ──────────────
        blob_path = _archive_raw_to_blob(getattr(lead, "job_id", None),
                                         getattr(lead, "id", None), research)

        # ── Log the scrape (guarded) ─────────────────────────────────────────
        _safe_clog_scrape(clog, research, clean_text=clean_text,
                          blob_path=blob_path, started=t_scrape)

        # ── 3️⃣  STORE Layer B: clean text → SQL (own isolated session so it
        #         never poisons the parent process_lead transaction) ──────────
        scrape_id = None
        try:
            with db_session() as embed_db:
                clean_text_db = clean_text[:4000] if len(clean_text) > 4000 else clean_text
                scrape = CompanyScrape(
                    job_id=getattr(lead, "job_id", None),
                    lead_id=getattr(lead, "id", None),
                    company_name=company,
                    website_url=source_url,
                    clean_text=clean_text_db,
                    blob_path=blob_path,
                    embedded=0,
                )
                embed_db.add(scrape)
                embed_db.flush()
                scrape_id = scrape.id
            # session committed here — embed task can find the row immediately
        except Exception as e:
            logger.warning(f"CompanyScrape persist failed for '{company}': {e}")

        # ── Dispatch async embedding (guarded so a broker blip doesn't bubble)
        if scrape_id:
            try:
                embed_company.delay(scrape_id)
                logger.info(
                    f"Dispatched embed for '{company}' "
                    f"(job={getattr(lead, 'job_id', '?')}, source={research.get('source')})"
                )
            except Exception as e:
                logger.warning(f"embed dispatch failed for '{company}': {e}")

    except Exception as e:
        logger.warning(
            f"persist_and_dispatch_embed skipped for lead "
            f"{getattr(lead, 'id', '?')}: {e}"
        )

def _safe_clog_scrape(clog, research, clean_text, blob_path, started) -> None:
    """Call clog.scrape() without ever letting a logging error bubble up."""
    if not clog:
        return
    try:
        clog.scrape(research, clean_text=clean_text,
                    source="app/scraper/web_scraper.py",
                    blob_path=blob_path, started=started)
    except Exception as e:
        logger.debug(f"clog.scrape failed: {e}")


def _archive_raw_to_blob(job_id, lead_id, research: dict) -> str:
    """Store the raw scrape result in Blob; return blob path (or '')."""
    try:
        import json
        from azure.storage.blob import BlobServiceClient
        from app.config import settings

        payload = json.dumps(research, default=str)
        client = BlobServiceClient.from_connection_string(
            settings.AZURE_BLOB_CONNECTION_STRING
        )
        container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
        blob_name = f"scraped/{job_id}/{lead_id}/raw.json"
        container.get_blob_client(blob_name).upload_blob(
            payload.encode("utf-8"), overwrite=True
        )
        return blob_name
    except Exception as e:
        logger.warning(f"Blob archive failed for lead {lead_id}: {e}")
        return ""
