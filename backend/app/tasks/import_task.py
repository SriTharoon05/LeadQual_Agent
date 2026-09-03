"""
tasks/import_task.py
────────────────────
Background file import. (NEW)

Keeps the /upload request instant regardless of file size or LLM
column-mapping latency by moving ALL heavy work off the request thread.

Flow:
  POST /upload  → store raw file to Blob, create Job(status="importing"),
                  return job_id IMMEDIATELY, dispatch import_file.delay(...)
  import_file   → download bytes → parse (+ LLM column map) → deduplicate →
                  bulk-insert leads → Job.status = "queued"  (ready for ICP)
                  on any failure → Job.status = "import_failed" + import_error

The frontend polls GET /jobs/{job_id}/status and watches for:
  "importing"     → still parsing/inserting (show a spinner)
  "queued"        → done; total_rows + duplicates_removed are populated
  "import_failed" → show job.import_error

Wire-in:
  1. Register in celery_app.py:
       include=["app.tasks.tasks", "app.tasks.embed_task", "app.tasks.import_task"]
  2. Dispatch from /upload:  import_file.delay(job_id, blob_name, suffix, filename)
"""

import logging
import uuid

from app.celery_app import celery
from app.database import db_session
from app.models import Job, Lead
from app.parsers.excel_parser import parse_excel
from app.parsers.csv_parser import parse_csv
from app.utils.deduplicator import deduplicate
from app.config import settings

logger = logging.getLogger(__name__)

MAX_NOT_FOUND_RETRIES = 5


# ── Safe cell reader (pandas fills missing values with NaN) ───────────────────
def _cell(row: dict, key: str) -> str:
    val = row.get(key, "")
    if val is None:
        return ""
    if isinstance(val, float) and val != val:   # NaN is the only value != itself
        return ""
    return str(val).strip()


# ── Task ──────────────────────────────────────────────────────────────────────
@celery.task(
    name="app.tasks.import_task.import_file",
    bind=True,
    max_retries=5,
    default_retry_delay=15,
    acks_late=True,
)
def import_file(self, job_id: str, blob_name: str, suffix: str, filename: str):
    """Parse → map → dedup → bulk-insert. Marks the job queued or import_failed."""
    with db_session() as db:
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
        except Exception as e:
            logger.error(f"import_file: DB load failed for {job_id}: {e}")
            raise self.retry(exc=e, countdown=15)

        if not job:
            # Parent /upload transaction may not have committed yet — retry a few
            # times, then give up rather than looping forever.
            if self.request.retries >= MAX_NOT_FOUND_RETRIES:
                logger.error(f"import_file: job {job_id} not found after retries — giving up")
                return
            logger.warning(f"import_file: job {job_id} not found yet — retrying")
            raise self.retry(countdown=15)

        try:
            raw_bytes = _download_blob(blob_name)
            if not raw_bytes:
                _fail(db, job, "Could not read the uploaded file from storage.")
                return

            # ── Parse (this is where the LLM column-mapping call happens) ─────
            if suffix in (".xlsx", ".xls", ".xlsm"):
                df = parse_excel(raw_bytes, job_id=job_id)
            else:
                df = parse_csv(raw_bytes, job_id=job_id)

            # ── Deduplicate ───────────────────────────────────────────────────
            df, dedup_stats = deduplicate(df)
            if len(df) == 0:
                _fail(db, job, "File contains no valid leads after deduplication.")
                return

            # ── Bulk-insert leads (single round-trip, not 500 add() calls) ────
            records = df.to_dict("records")
            mappings = []
            for idx, row in enumerate(records):
                mappings.append({
                    "id":                   str(uuid.uuid4()),
                    "job_id":               job_id,
                    "row_index":            idx,
                    "contact_name":         _cell(row, "Contact Name"),
                    "job_title":            _cell(row, "Job Title"),
                    "contact_department":   _cell(row, "Contact Department"),
                    "contact_seniority":    _cell(row, "Contact Seniority"),
                    "linkedin_profile_url": _cell(row, "LinkedIn Profile URL"),
                    "email_address":        _cell(row, "Email Address"),
                    "phone_number":         _cell(row, "Phone Number"),
                    "direct_phone_number":  _cell(row, "Direct Phone Number"),
                    "contact_country":      _cell(row, "Contact Country"),
                    "contact_state_region": _cell(row, "Contact State/Region"),
                    "contact_city":         _cell(row, "Contact City"),
                    "company_name":         _cell(row, "Company Name"),
                    "company_website":      _cell(row, "Company Website"),
                    "company_linkedin_url": _cell(row, "Company LinkedIn URL"),
                    "industry":             _cell(row, "Industry"),
                    "sub_industry":         _cell(row, "Sub Industry"),
                    "employee_count":       _cell(row, "Employee Count"),
                    "annual_revenue":       _cell(row, "Annual Revenue"),
                    "company_description":  _cell(row, "Company Description"),
                    "technologies_used":    _cell(row, "Technologies Used"),
                    "company_country":      _cell(row, "Company Country"),
                    "company_state_region": _cell(row, "Company State/Region"),
                    "company_city":         _cell(row, "Company City"),
                    "company_hq_location":  _cell(row, "Company HQ Location"),
                    "hq_phone_number":      _cell(row, "HQ Phone Number"),
                    "crm_account_id":       _cell(row, "CRM Account ID"),
                    "crm_contact_id":       _cell(row, "CRM Contact ID"),
                    "lead_source":          _cell(row, "Lead Source"),
                    "notes":                _cell(row, "Notes"),
                    "processing_status":    "pending",
                })

            # Bound memory/parameter pressure while retaining executemany speed.
            for start in range(0, len(mappings), 2000):
                db.bulk_insert_mappings(Lead, mappings[start:start + 2000])

            # ── Mark ready for ICP ────────────────────────────────────────────
            job.total_rows = len(df)
            try:
                job.duplicates_removed = int(dedup_stats.get("duplicates_removed", 0))
            except Exception:
                job.duplicates_removed = 0
            job.import_error = None
            job.status = "queued"
            db.flush()

            logger.info(f"import_file: job {job_id} imported {len(df)} leads "
                        f"({job.duplicates_removed} duplicates removed)")

        except ValueError as e:
            # Parsing errors (bad file, unreadable) — user-facing, no retry.
            _fail(db, job, str(e))
        except Exception as e:
            logger.error(f"import_file failed for {job_id}: {e}")
            _fail(db, job, f"Import failed: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fail(db, job, message: str) -> None:
    try:
        job.status = "import_failed"
        job.import_error = (message or "Import failed")[:1000]
        db.flush()
        logger.warning(f"import_file: job {getattr(job,'id','?')} import_failed: {message}")
    except Exception as e:
        logger.error(f"_fail could not update job: {e}")


def _download_blob(blob_name: str) -> bytes | None:
    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING)
        container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
        return container.get_blob_client(blob_name).download_blob().readall()
    except Exception as e:
        logger.error(f"import_file: blob download failed for '{blob_name}': {e}")
        return None
