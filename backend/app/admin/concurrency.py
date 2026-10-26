"""A renewable global admission limit across qualification worker processes.

Worker pools provide a fixed 30-slot capacity. The saved SQL setting limits
actual lead starts across replicas; changing it never kills an in-flight call.
"""
import random
import threading
import time
from contextvars import ContextVar
from uuid import uuid4

from app.admin.settings import load_settings, redis_client
from app.config import settings

KEY = "ql:runtime:active-leases:v1"
_current = ContextVar("lead_runtime_lease", default=None)
TTL = max(180, int(settings.LLM_TIMEOUT_SECONDS * 2 + 120))
_warnings_lock = threading.Lock()
_last_warning = {}


class CapacityDeferred(Exception):
    pass


class RuntimeLeaseLost(Exception):
    pass


def assert_current_lease(*_args):
    lease = _current.get()
    if lease:
        lease.assert_owned()


def before_model_call():
    assert_current_lease()
    if _current.get() and redis_client().ttl("ql:runtime:provider-cooldown") > 0:
        raise CapacityDeferred("Azure rate-limit cooldown; saved stages will resume")


class LeadAdmission:
    def __init__(self):
        self.token = str(uuid4())
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None
        self.context_token = None

    def __enter__(self):
        try:
            value = load_settings()
            admitted = redis_client().eval("""
                local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
                redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
                if redis.call('EXISTS',KEYS[3])==1 then return 0 end
                local cfg=redis.call('GET',KEYS[2])
                if not cfg then return 0 end
                local limit=cjson.decode(cfg).concurrency
                if redis.call('ZCARD',KEYS[1])>=limit then return 0 end
                redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[1]); return 1
            """, 3, KEY, "ql:runtime:settings:v1", "ql:runtime:provider-cooldown", self.token, TTL)
        except Exception as exc:
            from app.admin.telemetry import current_context, publish_event
            from app.utils.db_availability import database_unavailable_reason
            context = current_context()
            scope = context.get("job_id", "system")
            now = time.monotonic()
            with _warnings_lock:
                report = now - _last_warning.get(scope, -60) >= 60
                if report:
                    if len(_last_warning) >= 512:
                        _last_warning.clear()
                    _last_warning[scope] = now
            if report:
                reason = database_unavailable_reason(exc) or "runtime settings / Redis unavailable"
                publish_event(f"Lead starts are paused ({reason}). Check the admin migration, database access and Redis. Saved work remains pending.",
                              component="runtime.admission", kind="service_unavailable")
            raise CapacityDeferred("Runtime settings or admission service is unavailable") from None
        if not admitted:
            raise CapacityDeferred("Waiting for an available processing slot")
        self.context_token = _current.set(self)
        self.thread = threading.Thread(target=self._renew, name="lead-admission-lease", daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def assert_owned(self):
        if self.lost.is_set():
            raise RuntimeLeaseLost("Processing admission lease was lost; work will retry")
        try:
            renewed = redis_client().eval("""
                local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
                local score=redis.call('ZSCORE',KEYS[1],ARGV[1])
                if not score or tonumber(score)<=now then return 0 end
                redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[1]); return 1
            """, 1, KEY, self.token, TTL)
        except Exception:
            renewed = False
        if not renewed:
            self.lost.set()
            raise RuntimeLeaseLost("Processing admission lease was lost; work will retry")

    def _renew(self):
        while not self.stop.wait(15):
            try:
                self.assert_owned()
            except RuntimeLeaseLost:
                return

    def __exit__(self, *_args):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        if self.context_token is not None:
            _current.reset(self.context_token)
        try:
            redis_client().zrem(KEY, self.token)
        except Exception:
            pass  # The bounded lease expires; never release another task's token.


def active_count():
    return int(redis_client().eval("""
        local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
        redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
        return redis.call('ZCARD',KEYS[1])
    """, 1, KEY))


def retry_delay():
    return random.uniform(3, 7)
