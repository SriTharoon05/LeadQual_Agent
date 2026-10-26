"""
utils/monitor.py
────────────────
Centralised issue monitor — ONE place to see every WARNING, ERROR, and
RATE-LIMIT across ALL workers, instead of hunting through 500 per-company logs.

It writes to a small set of root-level files under  Logs/_monitor/ :

    Logs/_monitor/monitor_<dd-mm-yyyy>_IST.log     ← all WARNING + ERROR + rate-limit
    Logs/_monitor/rate_limits_<dd-mm-yyyy>_IST.log ← ONLY rate-limit (429) events

Every line is structured and tells you exactly WHO and WHERE:

  13:22:06 IST | RATE_LIMIT | worker=Thread-7 host=DESKTOP | job=3f9a2b7c |
      lead=1a2b3c4d | company=Acme Corp | src=app/validators/web_researcher.py:142 |
      429 Too Many Requests — retry after 30s

HOW IT WORKS
────────────
1. configure_monitor() attaches ONE logging handler to the root logger that
   captures every record at WARNING level or above — from your code AND from
   libraries (openai, httpx, azure). Rate-limit lines are auto-detected and
   also copied to the rate-limit file.
2. bind_context(job_id=..., lead_id=..., company=...) stamps each log line with
   the lead being processed, so you know which worker/lead hit the issue.

WIRE-IN
───────
  # once at startup (e.g. in celery_app.py, after configure_logging):
  from app.utils.monitor import configure_monitor
  configure_monitor()

  # at the top of process_lead(), after you know the ids:
  from app.utils.monitor import bind_context
  bind_context(job_id=lead.job_id, lead_id=lead_id, company=lead.company_name)

  # (optional) explicit rate-limit logging where you catch a 429:
  from app.utils.monitor import log_rate_limit
  log_rate_limit("web_researcher", "429 from Azure OpenAI", retry_after=30)
"""

import logging
import os
import re
import socket
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

# ── Paths + time ──────────────────────────────────────────────────────────────
MONITOR_DIR = os.path.join(os.getcwd(), "Logs", "_monitor")
IST = timezone(timedelta(hours=5, minutes=30))
_HOST = socket.gethostname()[:20]

# Rotate each file at ~10 MB, keep 5 backups → never unbounded.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5

# Patterns that mark a record as a rate-limit event.
_RATE_LIMIT_RE = re.compile(
    r"(429|rate[\s_-]?limit|ratelimit|too many requests|"
    r"requests? per minute|tokens? per minute|\bRPM\b|\bTPM\b|retry[- ]after)",
    re.IGNORECASE,
)

# ── Per-task context (works across the threads worker pool) ───────────────────
_ctx_job:     ContextVar[str] = ContextVar("mon_job",     default="-")
_ctx_lead:    ContextVar[str] = ContextVar("mon_lead",    default="-")
_ctx_company: ContextVar[str] = ContextVar("mon_company", default="-")


def bind_context(job_id=None, lead_id=None, company=None) -> None:
    """Stamp subsequent log lines (this task/thread) with these identifiers."""
    from app.admin.telemetry import set_context
    set_context(job_id=job_id, lead_id=lead_id, company=company)
    if job_id is not None:
        _ctx_job.set(_short(job_id, 8))
    if lead_id is not None:
        _ctx_lead.set(_short(lead_id, 8))
    if company is not None:
        _ctx_company.set(_clip(str(company), 40))


def clear_context() -> None:
    _ctx_job.set("-"); _ctx_lead.set("-"); _ctx_company.set("-")


def _short(v, n):  # noqa
    s = str(v)
    return s[:n] if s else "-"


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


# ── Formatter: structured, IST, with worker + source ──────────────────────────
class _MonitorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(IST).strftime("%d-%m %H:%M:%S")
        worker = threading.current_thread().name[:14]
        # source file trimmed to the app/ package if possible
        src = record.pathname.replace("\\", "/")
        if "/app/" in src:
            src = "app/" + src.split("/app/", 1)[1]
        else:
            src = os.path.basename(src)
        src = f"{src}:{record.lineno}"

        kind = "RATE_LIMIT" if _is_rate_limit(record) else record.levelname
        msg = record.getMessage()

        # Append the exception reason (type + message) so the monitor shows WHY,
        # not just the log message — crucial when a rate-limit is raised, not typed.
        if record.exc_info:
            try:
                exc = record.exc_info[1]
                if exc is not None:
                    msg += f" | exc: {type(exc).__name__}: {exc}"
            except Exception:
                pass

        # Collapse newlines/tracebacks to keep the monitor scannable (one line).
        msg = _clip(msg.replace("\n", " ⏎ ").strip(), 500)

        return (
            f"{ts} IST | {kind:<10} | worker={worker} host={_HOST} | "
            f"job={_ctx_job.get()} | lead={_ctx_lead.get()} | "
            f"company={_ctx_company.get()} | src={src} | logger={record.name} | {msg}"
        )


def _is_rate_limit(record: logging.LogRecord) -> bool:
    try:
        text = record.getMessage()
    except Exception:
        text = str(getattr(record, "msg", ""))
    # Also inspect exception text if present.
    if record.exc_info:
        try:
            text += " " + str(record.exc_info[1])
        except Exception:
            pass
    return bool(_RATE_LIMIT_RE.search(text or ""))


# ── Filters ───────────────────────────────────────────────────────────────────
class _WarnAndAbove(logging.Filter):
    """Only let WARNING/ERROR/CRITICAL through to the monitor."""
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


class _RateLimitOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _is_rate_limit(record)


# ── One-time setup ────────────────────────────────────────────────────────────
_CONFIGURED = False
_lock = threading.Lock()


def configure_monitor(level: int = logging.WARNING) -> None:
    """
    Attach the central monitor to the root logger. Idempotent + crash-safe.
    Captures WARNING+ from ALL loggers (your code + openai/httpx/azure).
    """
    global _CONFIGURED
    with _lock:
        if _CONFIGURED:
            return
        try:
            os.makedirs(MONITOR_DIR, exist_ok=True)
            stamp = datetime.now(IST).strftime("%d-%m-%Y")
            fmt = _MonitorFormatter()

            # 1) Combined issues file (WARNING + ERROR + rate-limit).
            issues = RotatingFileHandler(
                os.path.join(MONITOR_DIR, f"monitor_{stamp}_IST.log"),
                maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8",
            )
            issues.setLevel(level)
            issues.addFilter(_WarnAndAbove())
            issues.setFormatter(fmt)

            # 2) Rate-limit-only file.
            ratelimits = RotatingFileHandler(
                os.path.join(MONITOR_DIR, f"rate_limits_{stamp}_IST.log"),
                maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8",
            )
            ratelimits.setLevel(logging.WARNING)
            ratelimits.addFilter(_RateLimitOnly())
            ratelimits.setFormatter(fmt)

            root = logging.getLogger()
            root.addHandler(issues)
            root.addHandler(ratelimits)
            # Ensure root passes WARNING+ through to our handlers.
            if root.level == logging.NOTSET or root.level > logging.WARNING:
                root.setLevel(logging.WARNING)

            _CONFIGURED = True
            logging.getLogger("ailead.monitor").info("central monitor configured")
        except Exception as e:  # never let monitor setup crash startup
            logging.getLogger(__name__).warning(f"configure_monitor failed: {e}")
            _CONFIGURED = True


# ── Explicit helpers ──────────────────────────────────────────────────────────
_mon = logging.getLogger("ailead.monitor")


def log_rate_limit(module: str, detail: str, retry_after=None) -> None:
    """Explicitly record a rate-limit event with rich context."""
    ra = f" | retry_after={retry_after}s" if retry_after is not None else ""
    _mon.warning(f"RATE LIMIT in {module}: {detail}{ra}")


def log_warning(module: str, detail: str) -> None:
    _mon.warning(f"{module}: {detail}")


def log_error(module: str, detail: str, exc: Exception | None = None) -> None:
    if exc is not None:
        _mon.error(f"{module}: {detail}: {exc}")
    else:
        _mon.error(f"{module}: {detail}")


def is_rate_limit_error(exc: Exception) -> bool:
    """True if an exception looks like an Azure/OpenAI rate-limit (429)."""
    try:
        if getattr(exc, "status_code", None) == 429:
            return True
        return bool(_RATE_LIMIT_RE.search(f"{type(exc).__name__} {exc}"))
    except Exception:
        return False
