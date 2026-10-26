"""Recognize connection outages and pace durable background reconciliation.

These local timers only control polling/logging. SQL remains the source of
truth; an unavailable database never authorizes changing a lead's state.
"""
import logging
import random
import re
import threading
import time

from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError

_warnings_lock = threading.Lock()
_warnings_at: dict[tuple[str, str, str], float] = {}


def database_unavailable_reason(exc: BaseException) -> str | None:
    """Return a safe category for known infrastructure errors, otherwise None.

    In particular, SQLSTATE 42000 alone includes real SQL programming errors
    and is never sufficient to classify an exception as an outage.
    """
    if isinstance(exc, SQLAlchemyTimeoutError):
        return "connection_pool_exhausted" if "QueuePool limit" in str(exc) else None
    if not isinstance(exc, DBAPIError):
        return None
    original = getattr(exc, "orig", None)
    diagnostic = str(original or "")
    if re.search(r"\(40615\)", diagnostic):
        return "azure_sql_firewall_denied"
    if re.search(r"\((?:18456|4060)\)", diagnostic):
        return "database_access_denied"
    if re.search(r"\((?:40197|40501|40613|10928|10929)\)", diagnostic):
        return "database_temporarily_unavailable"
    args = getattr(original, "args", ())
    state = str(args[0]).strip().upper() if args else ""
    if state.startswith("08") or state in {"HYT00", "HYT01"} or exc.connection_invalidated:
        return "database_connection_unavailable"
    if state == "28000":
        return "database_access_denied"
    if state in {"IM002", "IM003", "IM004", "IM005", "IM006"} or (
        "invalid connection string attribute" in diagnostic.lower()
        and re.search(r"SQLDriverConnect|SQLConnect", diagnostic, re.IGNORECASE)
    ):
        return "database_driver_configuration"
    return None


def database_retry_delay(attempt: int) -> float:
    ceiling = min(300.0, 60.0 * (2 ** min(max(int(attempt), 0), 3)))
    return random.uniform(ceiling * 0.8, ceiling)


def warn_database_unavailable(logger: logging.Logger, context: str, exc: BaseException) -> None:
    """One sanitized diagnostic per category/context per five minutes/process."""
    reason = database_unavailable_reason(exc)
    if not reason:
        return
    now = time.monotonic()
    key = (logger.name, context, reason)
    with _warnings_lock:
        previous = _warnings_at.get(key)
        if previous is not None and now - previous < 300:
            return
        if len(_warnings_at) >= 256:
            _warnings_at.clear()
        _warnings_at[key] = now
    logger.warning("%s paused (%s); saved work will retry after database access returns", context, reason)


class RecoveryBackoff:
    """Bound retries and duplicate diagnostics without consuming task attempts."""

    def __init__(self, label: str, logger: logging.Logger, interval: float = 15):
        self.label = label
        self.logger = logger
        self.interval = interval
        self.next_attempt_at = 0.0
        self.failures = 0
        self._last_warning_at = 0.0
        self._last_reason = None

    def ready(self) -> bool:
        return time.monotonic() >= self.next_attempt_at

    def succeeded(self) -> None:
        if self.failures:
            self.logger.info("%s resumed; saved pending requests are being reconciled", self.label)
        self.failures = 0
        self._last_reason = None
        self.next_attempt_at = time.monotonic() + self.interval

    def failed(self, exc: BaseException) -> None:
        self.failures += 1
        reason = database_unavailable_reason(exc)
        initial_delay = max(self.interval, 60 if reason else 15)
        ceiling = min(300.0, initial_delay * (2 ** min(self.failures - 1, 5)))
        delay = random.uniform(ceiling * 0.8, ceiling)
        now = time.monotonic()
        self.next_attempt_at = now + delay
        category = reason or type(exc).__name__
        if self.failures == 1 or category != self._last_reason or now - self._last_warning_at >= 300:
            if reason:
                self.logger.warning(
                    "%s paused (%s); retrying in %.0fs. Saved work remains pending. "
                    "Restore database network/access configuration if this persists.",
                    self.label, reason, delay,
                )
            else:
                self.logger.error("%s failed; retrying in %.0fs", self.label, delay, exc_info=True)
            self._last_warning_at = now
            self._last_reason = category
