"""
utils/logger.py
───────────────
Thin pipeline wrapper around the standard Python logger.
The root operational handler asynchronously persists only WARNING/ERROR.

Design principle: logging must NEVER crash the pipeline. Every message is
coerced to a safe non-empty string. Logging never enlists the lead transaction.

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
from typing import Optional

from sqlalchemy.orm import Session

from app.admin.telemetry import set_context

# Standard Python logger (goes to stdout / your log aggregator)
_py_logger = logging.getLogger("ailead.pipeline")



# ── Safe string coercion ──────────────────────────────────────────────────────
def _safe_str(message, default: str = "(empty log message)") -> str:
    """
    Coerce any message to a non-empty string.
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
    Bind full operational context and emit pipeline messages.
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
        set_context(job_id=job_id, lead_id=lead_id)

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
        msg = _safe_str(message)
        console_message = f"{msg}: {exc}" if exc is not None else msg
        _py_logger.error(self._fmt(console_message))

    # Allow updating IDs mid-task (e.g. after the lead row is inserted)
    def set_lead_id(self, lead_id: str) -> None:
        self._lead_id = lead_id
        set_context(lead_id=lead_id)

    def set_job_id(self, job_id: str) -> None:
        self._job_id = job_id
        set_context(job_id=job_id)

    # ── Internal ──────────────────────────────────────────────────────────────
    def _write(self, level: str, message: str) -> None:
        # Root OperationalHandler persists warnings/errors through its Redis
        # outbox. INFO remains console-only; do not duplicate SQL log writes.
        return

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
