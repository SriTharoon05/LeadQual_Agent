"""Bounded UI diagnostics and a Redis outbox for asynchronous SQL persistence.

Only warnings/errors enter the event feed. Usage carries its own immutable UUID,
so replay after a worker restart cannot count a charge twice.
"""
import inspect
import atexit
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.admin.settings import redis_client, utcnow

STREAM = "ql:telemetry:outbox:v1"
GROUP = "sql-persistence-v1"
_context = ContextVar("operational_context", default={})
_recent_rate_limit = ContextVar("recent_provider_rate_limit", default=None)
_fallback = queue.Queue(maxsize=2000)
_events = queue.Queue(maxsize=2000)
_thread = None
_publisher = None
_pid = None
_start_lock = threading.Lock()
_handler = None


def set_context(**values):
    current = dict(_context.get())
    current.update({key: str(value) for key, value in values.items() if value is not None})
    _context.set(current)


def clear_context():
    _context.set({})
    _recent_rate_limit.set(None)


def mark_rate_limit():
    _recent_rate_limit.set(time.monotonic())


def current_context():
    return dict(_context.get())


def valid_id(value):
    try:
        return str(UUID(str(value))) if value else None
    except (ValueError, TypeError):
        return None


def safe_message(value):
    text = str(value or "Operation reported a warning")
    if "[SQL:" in text or "SQLDriverConnect" in text:
        return "Database operation failed. Check database connectivity, access and the server diagnostic log."
    text = re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[redacted]', text)
    text = re.sub(r'''(?i)((?:api[-_ ]?key|password|pwd|accountkey|authorization|access_token|sig)["']?\s*[=:]\s*["']?)[^\s,;}\]"']+''', r'\1[redacted]', text)
    return re.sub(r'\s+', ' ', text).strip()[:1800]


def enqueue(kind, data, *, require_durable=False):
    packet = {"type": kind, "data": data}
    try:
        client = redis_client()
        pipe = client.pipeline(transaction=True)
        pipe.xadd(STREAM, {"payload": json.dumps(packet, ensure_ascii=False)})
        if kind == "event":
            encoded = json.dumps(data, ensure_ascii=False)
            keys = ["ql:events:recent:v1"]
            if data.get("job_id"):
                keys.append("ql:events:job:" + data["job_id"])
            for key in keys:
                pipe.lpush(key, encoded)
                pipe.ltrim(key, 0, 499)
                pipe.expire(key, 604800)
        pipe.execute()
        return True
    except Exception:
        if require_durable:
            _persist([packet])
            return True
        try:
            _fallback.put_nowait(packet)
        except queue.Full:
            sys.stderr.write("Operational telemetry buffer is full; usage reporting may be incomplete.\n")


def publish_event(message, *, severity="WARNING", kind=None, stage=None, component=None,
                  job_id=None, lead_id=None, retry_after=None):
    context = current_context()
    text = safe_message(message)
    is_rate = bool(re.search(r'\b429\b|rate.?limit|too many requests', text, re.I))
    data = {"event_id": str(uuid4()), "job_id": valid_id(job_id or context.get("job_id")),
        "lead_id": valid_id(lead_id or context.get("lead_id")),
        "severity": "ERROR" if severity == "ERROR" else "WARNING",
        "kind": kind or ("rate_limit" if is_rate else "error" if severity == "ERROR" else "warning"),
        "stage": str(stage or context.get("stage") or "")[:50] or None,
        "component": str(component or context.get("component") or "application")[:100],
        "message": text, "retry_after": retry_after,
        "occurred_at": utcnow().isoformat()}
    try:
        _events.put_nowait(data)
    except queue.Full:
        sys.stderr.write("Warning/error publication buffer is full; check worker diagnostics.\n")


class OperationalHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.busy = threading.local()

    def emit(self, record):
        if getattr(self.busy, "active", False) or threading.current_thread().name == "usage-persistence":
            return
        self.busy.active = True
        try:
            message = record.getMessage()
            recent = _recent_rate_limit.get()
            if recent is not None and time.monotonic() - recent < 3 and re.search(r'\b429\b|rate.?limit|too many requests', message, re.I):
                return  # The metered attempt already emitted its structured event.
            publish_event(message, severity="ERROR" if record.levelno >= logging.ERROR else "WARNING",
                          component=record.name)
        except Exception:
            pass
        finally:
            self.busy.active = False


def _persist(packets):
    from sqlalchemy import BigInteger, cast, func, update
    from sqlalchemy.exc import IntegrityError
    from app.database import SessionLocal
    from app.models import Job, Lead, LLMCallLog, OperationalEvent
    with SessionLocal() as db:
        usage_ids = [item["data"]["id"] for item in packets if item["type"] == "usage"]
        event_ids = [item["data"]["event_id"] for item in packets if item["type"] == "event"]
        known_usage = {str(row[0]): row[1] for row in db.query(LLMCallLog.id, LLMCallLog.status).filter(LLMCallLog.id.in_(usage_ids)).all()} if usage_ids else {}
        pending_usage = {}
        known_events = {row[0] for row in db.query(OperationalEvent.event_id).filter(OperationalEvent.event_id.in_(event_ids)).all()} if event_ids else set()
        job_ids = {item["data"].get("job_id") for item in packets if item["type"] == "usage" and item["data"].get("job_id")}
        lead_ids = {item["data"].get("lead_id") for item in packets if item["type"] == "usage" and item["data"].get("lead_id")}
        valid_jobs = {str(row[0]) for row in db.query(Job.id).filter(Job.id.in_(job_ids)).all()} if job_ids else set()
        valid_leads = {str(row[0]) for row in db.query(Lead.id).filter(Lead.id.in_(lead_ids)).all()} if lead_ids else set()
        for packet in packets:
            data = dict(packet["data"])
            if packet["type"] == "usage":
                existing = data["id"] in known_usage
                if existing and (data.get("status") == "started" or known_usage[data["id"]] != "started"):
                    continue
                for name in ("cost_input_usd", "cost_cached_usd", "cost_output_usd", "cost_search_usd", "cost_total_usd"):
                    if data.get(name) is not None:
                        data[name] = Decimal(str(data[name]))
                if data.get("created_at"):
                    data["created_at"] = datetime.fromisoformat(data["created_at"])
                # Deleted uploads must not poison the outbox with FK failures.
                if data.get("job_id") and data["job_id"] not in valid_jobs:
                    continue
                if data.get("lead_id") and data["lead_id"] not in valid_leads:
                    data["lead_id"] = None
                if existing:
                    pending = pending_usage.get(data["id"])
                    if pending is not None:
                        for name, value in data.items():
                            setattr(pending, name, value)
                    else:
                        db.execute(update(LLMCallLog).where(LLMCallLog.id == data["id"],
                            LLMCallLog.status == "started").values(**{name: value for name, value in data.items() if name != "id"}))
                else:
                    pending = LLMCallLog(**data)
                    db.add(pending)
                    pending_usage[data["id"]] = pending
                known_usage[data["id"]] = data.get("status")
            elif packet["type"] == "event":
                if data["event_id"] in known_events:
                    continue
                known_events.add(data["event_id"])
                data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
                db.add(OperationalEvent(**data))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise  # A racing consumer committed first; replay is idempotent.
        if job_ids:
            # Roll up after committing child usage rows: taking a parent write
            # lock inside the FK insert transaction can deadlock other writers.
            locks = db.query(Job.id).filter(Job.id.in_(job_ids)).order_by(Job.id)
            if db.bind.dialect.name == "mssql":
                locks = locks.with_hint(Job, "WITH (UPDLOCK, ROWLOCK)", dialect_name="mssql")
            current_jobs = [str(row[0]) for row in locks.all()]
            totals = db.query(LLMCallLog.job_id,
                func.coalesce(func.sum(cast(LLMCallLog.prompt_tokens, BigInteger)), 0),
                func.coalesce(func.sum(cast(LLMCallLog.completion_tokens, BigInteger)), 0),
                func.coalesce(func.sum(cast(LLMCallLog.total_tokens, BigInteger)), 0)
            ).filter(LLMCallLog.job_id.in_(current_jobs)).group_by(LLMCallLog.job_id).all() if current_jobs else []
            for job_id, prompt, completion, total in totals:
                db.execute(update(Job).where(Job.id == str(job_id)).values(
                    prompt_tokens=prompt, completion_tokens=completion, total_tokens_used=total))
            db.commit()


def _run():
    consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    held, packets = [], []
    attempts = 0
    while True:
        try:
            client = redis_client()
            if not packets:
                try:
                    client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
                except Exception as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise
                pending = client.xpending_range(STREAM, GROUP, "-", "+", 50)
                stale = [row["message_id"] for row in pending if row["time_since_delivered"] >= 120000]
                messages = client.xclaim(STREAM, GROUP, consumer, 120000, stale) if stale else []
                if not messages:
                    batch = client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=50, block=1000)
                    messages = batch[0][1] if batch else []
                held = [message_id for message_id, _ in messages]
                packets = [json.loads(fields["payload"]) for _, fields in messages]
                while len(packets) < 50:
                    try:
                        packets.append(_fallback.get_nowait())
                    except queue.Empty:
                        break
            if packets:
                _persist(packets)
                if held:
                    pipe = client.pipeline(transaction=True)
                    pipe.xack(STREAM, GROUP, *held)
                    pipe.xdel(STREAM, *held)
                    pipe.execute()
                held, packets = [], []
            attempts = 0
        except Exception:
            attempts += 1
            if attempts == 1 or attempts % 20 == 0:
                sys.stderr.write("Usage/log persistence delayed; telemetry remains queued for retry.\n")
            # If Redis alone is down, persist the small local fallback directly.
            if not packets:
                try:
                    packets = [_fallback.get_nowait()]
                except queue.Empty:
                    pass
            if packets and not held:
                try:
                    _persist(packets)
                    packets = []
                except Exception:
                    pass
            time.sleep(min(30, 2 ** min(attempts, 5)))


def _publish_events():
    while True:
        data = _events.get()
        try:
            enqueue("event", data)
        finally:
            _events.task_done()


def flush_events(timeout=5):
    """Give local diagnostics a bounded chance to reach storage on clean exit."""
    deadline = time.monotonic() + timeout
    while _events.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


atexit.register(flush_events)


def configure_telemetry():
    global _thread, _publisher, _pid, _handler
    with _start_lock:
        logging.captureWarnings(True)
        if _handler is None:
            _handler = OperationalHandler()
            logging.getLogger().addHandler(_handler)
        if _pid != os.getpid() or _thread is None or not _thread.is_alive():
            _pid = os.getpid()
            _thread = threading.Thread(target=_run, name="usage-persistence", daemon=True)
            _thread.start()
        if _publisher is None or not _publisher.is_alive():
            _publisher = threading.Thread(target=_publish_events, name="event-publication", daemon=True)
            _publisher.start()


def task_context(task=None, args=None, kwargs=None, **_extra):
    clear_context()
    try:
        values = inspect.signature(task.run).bind_partial(*(args or ()), **(kwargs or {})).arguments
        headers = task.request.headers or {}
        set_context(job_id=values.get("job_id") or headers.get("job_id"), lead_id=values.get("lead_id"),
                    stage=task.name.rsplit(".", 1)[-1], component=task.name)
    except Exception:
        pass
