"""
utils/logger.py
───────────────
Thin wrapper that writes structured entries to the processing_logs table
AND emits to the standard Python logger simultaneously. (HARDENED edition)

Design principle: logging must NEVER crash the pipeline. Every message is
coerced to a safe non-empty string; DB write failures are swallowed; a missing
DB session degrades to console-only logging.

Import pattern everywhere in the pipeline:
    from app.utils.logger import pipeline_logger

    log = pipeline_logger(db, job_id="...", lead_id="...")
    log.info("Stage 1 passed")
    log.warning("Missing phone number")
    log.error("LLM call failed", exc=e)

One-time setup (call once at worker/app startup):
    from app.utils.logger import configure_logging
    configure_logging()
"""

import logging
import traceback
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.models import ProcessingLog

# Standard Python logger (goes to stdout / your log aggregator)
_py_logger = logging.getLogger("ailead.pipeline")

# Cap DB messages so a runaway payload can't bloat the table / hit column limits.
MAX_DB_MESSAGE_CHARS = 20000


# ── Safe string coercion ──────────────────────────────────────────────────────
def _safe_str(message, default: str = "(empty log message)") -> str:
    """
    Coerce ANY message to a non-empty string. Fixes the NOT-NULL crash when a
    None or non-string message reaches ProcessingLog(message=...).
    """
    if message is None:
        return default
    if isinstance(message, str):
        return message if message.strip() else default
    try:
        s = str(message)
        return s if s.strip() else default
    except Exception:
        return default


# ── One-time logging setup ────────────────────────────────────────────────────
_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure root + third-party loggers for clean pipeline output.
    Idempotent — safe to call more than once. Never raises.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    try:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
        # Silence libraries that dump raw HTTP URLs on every LLM call.
        for noisy in ("httpx", "httpcore", "openai", "urllib3", "azure", "azure.core"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _py_logger.setLevel(level)
    except Exception as e:  # pragma: no cover
        # Logging setup must never take down startup.
        logging.getLogger(__name__).warning(f"configure_logging failed: {e}")
    finally:
        _CONFIGURED = True


class PipelineLogger:
    """
    Thin dual-writer: Azure SQL processing_logs + Python logging.
    Instantiate once per task with the IDs you know at that point.
    """

    def __init__(
        self,
        db: Optional[Session],
        job_id: Optional[str] = None,
        lead_id: Optional[str] = None,
    ) -> None:
        self._db = db
        self._job_id = job_id
        self._lead_id = lead_id

    # ── Public methods ────────────────────────────────────────────────────────
    def info(self, message: str) -> None:
        msg = _safe_str(message)
        self._write("INFO", msg)
        _py_logger.info(self._fmt(msg))

    def warning(self, message: str) -> None:
        msg = _safe_str(message)
        self._write("WARNING", msg)
        _py_logger.warning(self._fmt(msg))

    def error(self, message: str, exc: Optional[Exception] = None) -> None:
        # DB entry gets the full traceback; console stays clean (one-line reason).
        msg = _safe_str(message)
        db_message = msg
        console_message = msg

        if exc is not None:
            try:
                tb = traceback.format_exc()
            except Exception:
                tb = ""
            if tb and tb.strip() and tb.strip() != "NoneType: None":
                db_message = f"{msg}\n{tb}"
            console_message = f"{msg}: {exc}"

        self._write("ERROR", db_message)
        _py_logger.error(self._fmt(console_message))

    # Allow updating IDs mid-task (e.g. after the lead row is inserted)
    def set_lead_id(self, lead_id: str) -> None:
        self._lead_id = lead_id

    def set_job_id(self, job_id: str) -> None:
        self._job_id = job_id

    # ── Internal ──────────────────────────────────────────────────────────────
    def _write(self, level: str, message: str) -> None:
        # No DB session → console-only (still logged by the public methods).
        if self._db is None:
            return
        try:
            entry = ProcessingLog(
                job_id=self._job_id,
                lead_id=self._lead_id,
                level=level,
                message=_safe_str(message)[:MAX_DB_MESSAGE_CHARS],
            )
            # Never enlist logs in the business transaction. A flush here used
            # to retain FK locks throughout DNS/AI waits and could leave the
            # caller's Session unusable after a best-effort logging failure.
            with engine.connect() as connection:
                try:
                    # A caller can still hold an uncommitted parent-row write.
                    # Logging must not wait indefinitely for that same caller.
                    connection.exec_driver_sql("SET LOCK_TIMEOUT 1000")
                    connection.commit()
                    with SessionLocal(bind=connection) as log_db:
                        log_db.add(entry)
                        log_db.commit()
                finally:
                    try:
                        connection.rollback()
                        # SQL Server SET options survive transaction rollback;
                        # restore the default before returning this socket.
                        connection.exec_driver_sql("SET LOCK_TIMEOUT -1")
                        connection.commit()
                    except Exception:
                        connection.invalidate()
                        raise
        except Exception as e:
            # Never let logging blow up the pipeline.
            try:
                _py_logger.error(f"Failed to write processing_log to DB: {e}")
            except Exception:
                pass

    def _fmt(self, message: str) -> str:
        parts = []
        if self._job_id:
            parts.append(f"job={str(self._job_id)[:8]}")
        if self._lead_id:
            parts.append(f"lead={str(self._lead_id)[:8]}")
        prefix = f"[{' '.join(parts)}] " if parts else ""
        return f"{prefix}{message}"


# ── Factory function (preferred entry point) ──────────────────────────────────
def pipeline_logger(
    db: Optional[Session],
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> PipelineLogger:
    """
    Create a PipelineLogger bound to a DB session and optional IDs.

    Usage:
        with db_session() as db:
            log = pipeline_logger(db, job_id=job.id)
            log.info("Processing started")
    """
    return PipelineLogger(db=db, job_id=job_id, lead_id=lead_id)
