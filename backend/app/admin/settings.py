"""SQL is authoritative; Redis holds a short-lived, versioned settings cache."""
import json
import os
import threading
from datetime import datetime, timezone

import redis

from app.config import settings

CACHE_KEY = "ql:runtime:settings:v1"
DEFAULT_PRICING = {
    "model": "gpt-4.1-mini", "input_per_million": 0.40,
    "cached_per_million": 0.10, "output_per_million": 1.60,
    "embedding_model": "text-embedding-ada-002", "embedding_per_million": 0.10,
    "search_per_thousand": 14.0, "confirmed": False,
    "basis": "Reference USD rates; confirm your Azure region, deployment tier and contract before treating estimates as budget figures.",
    "token_source": "https://azure.microsoft.com/en-us/pricing/details/azure-openai/",
    "search_source": "https://www.microsoft.com/en-us/bing/apis",
}
_redis = None
_pid = None
_lock = threading.Lock()


def redis_client():
    global _redis, _pid
    if _redis is None or _pid != os.getpid():
        with _lock:
            if _redis is None or _pid != os.getpid():
                _redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2, max_connections=80)
                _pid = os.getpid()
    return _redis


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def settings_dict(row):
    return {"concurrency": row.concurrency, "version": row.version,
        "pricing": json.loads(row.pricing_json), "usd_inr": row.usd_inr,
        "fx_as_of": row.fx_as_of.isoformat() + "Z" if row.fx_as_of else None,
        "fx_source": row.fx_source, "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None}


def cache_settings(value):
    # A reader that started before Save must not replace a newer cache version.
    redis_client().eval("""
        local watermark=tonumber(redis.call('GET',KEYS[2]) or '-1')
        if watermark > tonumber(ARGV[2]) then return 0 end
        redis.call('SET',KEYS[2],ARGV[2])
        redis.call('SET',KEYS[1],ARGV[1],'EX',5)
        return 1
    """, 2, CACHE_KEY, CACHE_KEY + ":version", json.dumps(value), value["version"])


def load_settings():
    client = redis_client()
    cached = client.get(CACHE_KEY)
    if cached:
        value = json.loads(cached)
        if isinstance(value.get("concurrency"), int) and 1 <= value["concurrency"] <= 30:
            return value
    from app.database import SessionLocal
    from app.models import RuntimeSettings
    with SessionLocal() as db:
        row = db.get(RuntimeSettings, "runtime")
        if row is None:
            raise RuntimeError("Run the admin operations migration before starting workers")
        value = settings_dict(row)
    cache_settings(value)
    return value
