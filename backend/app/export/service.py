"""Shared export coordination. Redis stores small states, Blob stores files.

No local file paths or bearer tokens are exposed. Cache revisions cover saved
results, reviewed state/notes and refresh activity; downloads are snapshots.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import uuid4

import jwt
import redis
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from sqlalchemy import BigInteger, case, cast, func

from app.config import settings
from app.models import FinalScore, Job, Lead, LeadRefresh, LeadTracking
from app.scoring.policy import SCORING_VERSION

logger = logging.getLogger(__name__)
EXPORT_VERSION = "streaming-v3-admin-" + SCORING_VERSION
STATE_TTL = 86_400
LEASE_SECONDS = 90
WORKERS_KEY = "ql:exports:workers"


class ExportWorkerUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def store():
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=3, health_check_interval=30)


def state_key(export_id):
    return "ql:exports:state:" + export_id


def lease_key(export_id):
    return "ql:exports:lease:" + export_id


def get_state(export_id):
    raw = store().get(state_key(export_id))
    return json.loads(raw) if raw else None


def source_revision(db, job):
    """Small SQL aggregates, not a scan of multi-megabyte evidence JSON."""
    leads = db.query(func.count(Lead.id), func.max(Lead.evidence_checked_at),
        func.coalesce(func.sum(cast(LeadTracking.version, BigInteger)), 0),
        func.max(LeadTracking.updated_at)
    ).select_from(Lead).outerjoin(LeadTracking, LeadTracking.id == Lead.tracking_id
    ).filter(Lead.job_id == job.id).one()
    scores = db.query(func.count(FinalScore.id), func.max(FinalScore.created_at)
    ).join(Lead, Lead.id == FinalScore.lead_id).filter(Lead.job_id == job.id).one()
    refreshes = db.query(func.count(LeadRefresh.id), func.max(LeadRefresh.created_at),
        func.max(LeadRefresh.started_at), func.max(LeadRefresh.finished_at),
        func.sum(case((LeadRefresh.status == "queued", 1), else_=0)),
        func.sum(case((LeadRefresh.status == "running", 1), else_=0)),
        func.sum(case((LeadRefresh.status == "failed", 1), else_=0))
    ).join(Lead, Lead.id == LeadRefresh.lead_id).filter(Lead.job_id == job.id).one()
    values = [EXPORT_VERSION, str(job.id), job.status, job.icp_prompt, job.completed_at,
              list(leads), list(scores), list(refreshes)]
    digest = hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()
    return digest, int(leads[0])


def cache_key(job_id, format, revision):
    return f"ql:exports:cache:{job_id}:{format}:{revision}"


def active_key(job_id, format):
    return f"ql:exports:active:{job_id}:{format}"


def release_key(key, expected):
    store().eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                 1, key, expected)


def update_owned(export_id, token, **changes):
    """Fence stale workers and extend their renewable lease atomically."""
    changes["heartbeat_at"] = time.time()
    result = store().eval("""
        if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        local raw = redis.call('GET', KEYS[2]); if not raw then return 0 end
        local state = cjson.decode(raw)
        for k,v in pairs(cjson.decode(ARGV[2])) do state[k] = v end
        redis.call('SET', KEYS[2], cjson.encode(state), 'EX', ARGV[3])
        redis.call('EXPIRE', KEYS[1], ARGV[4]); return 1
    """, 2, lease_key(export_id), state_key(export_id), token,
        json.dumps(changes), STATE_TTL, LEASE_SECONDS)
    if not result:
        raise RuntimeError("Export lease expired; a later worker owns this request")


def request_export(db, job, format):
    revision, total = source_revision(db, job)
    if not total:
        raise ValueError("This upload has no saved lead rows to export.")
    index = cache_key(str(job.id), format, revision)
    busy = active_key(str(job.id), format)
    for _ in range(3):
        proposed = str(uuid4())
        state = {"export_id": proposed, "job_id": str(job.id), "format": format,
            "status": "queued", "phase": "Waiting for export worker", "rows_written": 0,
            "total_rows": total, "created_at": time.time(), "heartbeat_at": time.time(),
            "revision": revision, "cache_key": index, "attempts": 0}
        chosen = store().eval("""
            local existing = redis.call('GET', KEYS[1])
            if existing then return existing end
            local active = redis.call('GET', KEYS[3])
            if active then return active end
            redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
            redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
            redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[3]); return ARGV[1]
        """, 3, index, state_key(proposed), busy, proposed, json.dumps(state), STATE_TTL)
        state = get_state(chosen)
        if not state or state["status"] == "failed":
            release_key(index, chosen)
            release_key(busy, chosen)
            continue
        if state["status"] == "ready" and state["revision"] != revision:
            release_key(busy, chosen)
            release_key(index, chosen)
            continue
        if state["status"] == "ready":
            release_key(busy, chosen)
            if time.time() - state.get("ready_at", 0) > settings.EXPORT_CACHE_SECONDS:
                release_key(index, chosen)
                continue
        ensure_dispatched(state)
        return get_state(chosen)
    raise ValueError("Export requests changed concurrently. Please try again.")


def ensure_dispatched(state):
    """Polling recovers lost publication/worker crashes without duplicate builds."""
    if state["status"] in ("ready", "failed"):
        return
    export_id = state["export_id"]
    if time.time() - state["created_at"] > settings.EXPORT_TIMEOUT_SECONDS + 300:
        # Never overwrite a live owner; it enforces its own deadline.
        store().eval("""
            if redis.call('EXISTS',KEYS[1]) == 1 then return 0 end
            local raw=redis.call('GET',KEYS[2]); if not raw then return 0 end
            local s=cjson.decode(raw)
            if s.status == 'ready' or s.status == 'failed' then return 0 end
            s.status='failed'; s.error='Export timed out. Check that an export worker is running, then retry.'
            redis.call('SET',KEYS[2],cjson.encode(s),'EX',ARGV[1]); return 1
        """, 2, lease_key(export_id), state_key(export_id), STATE_TTL)
        return
    if store().exists(lease_key(export_id)):
        return
    store().zremrangebyscore(WORKERS_KEY, "-inf", time.time() - 90)
    if not store().zcard(WORKERS_KEY):
        raise ExportWorkerUnavailable(f"The export worker is offline. Start a worker for the {settings.CELERY_EXPORT_QUEUE} queue, then retry.")
    if not store().set("ql:exports:dispatch:" + export_id, "1", nx=True, ex=30):
        return
    try:
        from app.tasks.export_task import build_export_file
        build_export_file.apply_async(args=[export_id], task_id="export-" + export_id,
            queue=settings.CELERY_EXPORT_QUEUE, retry=False)
    except Exception:
        store().delete("ql:exports:dispatch:" + export_id)
        raise


def blob_service(metadata=False):
    return BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING,
        connection_timeout=3 if metadata else 10, read_timeout=5 if metadata else 60,
        retry_total=0 if metadata else 3,
        max_single_get_size=1_048_576, max_chunk_get_size=1_048_576)


def download_ticket(state):
    now = datetime.now(timezone.utc)
    token = jwt.encode({"iss": "quality-lead-export", "aud": "lead-export-download",
        "iat": now, "exp": now + timedelta(minutes=15), "purpose": "download",
        "job_id": state["job_id"], "export_id": state["export_id"]},
        settings.AUTH_SECRET_KEY, algorithm="HS256")
    return f"/jobs/{state['job_id']}/exports/{state['export_id']}/file?ticket={token}"


def verify_ticket(token, job_id, export_id):
    claims = jwt.decode(token, settings.AUTH_SECRET_KEY, algorithms=["HS256"],
        issuer="quality-lead-export", audience="lead-export-download",
        options={"require": ["iss", "aud", "iat", "exp", "purpose", "job_id", "export_id"]})
    if (claims["purpose"] != "download" or claims["job_id"] != job_id
            or claims["export_id"] != export_id):
        raise jwt.InvalidTokenError("Ticket does not permit this download")


def public_status(state):
    result = {key: state.get(key) for key in (
        "export_id", "job_id", "format", "status", "phase", "rows_written", "total_rows",
        "created_at", "ready_at", "snapshot_at", "error", "size_bytes", "filename")}
    if state["status"] == "ready":
        try:
            with blob_service(metadata=True) as service:
                service.get_blob_client(settings.AZURE_BLOB_CONTAINER, state["blob_name"]).get_blob_properties()
        except ResourceNotFoundError:
            result.update(status="failed", error="The export file expired. Please create a new export.")
            release_key(state["cache_key"], state["export_id"])
            release_key(active_key(state["job_id"], state["format"]), state["export_id"])
            return result
        result["download_path"] = download_ticket(state)
    return result
