"""Celery queues with fixed execution capacity and SQL-controlled admission.

Start qualification workers with --pool=threads --concurrency=30. The global
1..30 active-lead limit is saved through /admin/settings and enforced by Redis
leases across replicas. MAX_WORKERS is a deprecated, unused environment field.
Run imports, embeddings and exports in their dedicated queues.
"""

import logging

from celery import Celery
from celery.signals import worker_ready, worker_process_init, worker_process_shutdown, worker_shutdown, task_prerun, task_postrun

from app.config import settings, CELERY_QUEUE_NAME

logger = logging.getLogger(__name__)



# ── Logging setup (guarded — never let logging config crash startup) ──────────
try:
    from app.utils.logger import configure_logging
    configure_logging()
    from app.utils.monitor import configure_monitor
    configure_monitor()
except Exception as e:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).warning(f"logging/monitor setup failed: {e}")


# ── Celery app ────────────────────────────────────────────────────────────────
celery = Celery(
    "ailead",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.tasks", "app.tasks.embed_task", "app.tasks.import_task",
             "app.tasks.refresh_task", "app.tasks.export_task", "app.tasks.dispatch_task"],
)

# Execution capacity is fixed. SQL runtime_settings controls the global count of
# active lead tasks through renewable admission leases, including across replicas.
# MAX_WORKERS is retained as an unused legacy environment field.
_concurrency = 30

celery.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Routing — all tasks go to the lead_processing queue
    task_default_queue=CELERY_QUEUE_NAME,
    task_routes={
        "app.tasks.tasks.*":      {"queue": CELERY_QUEUE_NAME},
        "app.tasks.import_task.*": {"queue": settings.CELERY_IMPORT_QUEUE},
        "app.tasks.embed_task.*": {"queue": settings.CELERY_EMBED_QUEUE},
        "app.tasks.refresh_task.*": {"queue": CELERY_QUEUE_NAME},
        "app.tasks.export_task.*": {"queue": settings.CELERY_EXPORT_QUEUE},
    },

    # Worker behaviour — threads pool suits this I/O-bound pipeline.
    worker_pool="threads",
    worker_concurrency=_concurrency,
    worker_max_tasks_per_child=200,      # recycle after 200 tasks (memory hygiene)
    task_acks_late=True,                 # ack only after task completes
    task_reject_on_worker_lost=True,     # requeue if worker dies mid-task
    worker_prefetch_multiplier=1,        # fair dispatch, one task per slot

    # Result expiry
    result_expires=86400,                # 24 h

    # Retry defaults (overridden per-task where needed)
    task_max_retries=3,
    task_default_retry_delay=30,         # seconds
    # Bound failed publish calls made by refresh polling/recovery.
    broker_connection_timeout=5,
    broker_transport_options={"socket_connect_timeout": 5, "socket_timeout": 5},
)


@worker_process_init.connect
def _operations_child(**_kwargs):
    from app.admin.telemetry import configure_telemetry
    configure_telemetry()


@task_prerun.connect
def _operations_task_start(**kwargs):
    from app.admin.telemetry import task_context
    task_context(**kwargs)


@task_postrun.connect
def _operations_task_end(**_kwargs):
    from app.admin.telemetry import clear_context
    from app.utils.monitor import clear_context as clear_monitor
    clear_context()
    clear_monitor()


@worker_shutdown.connect
@worker_process_shutdown.connect
def _operations_stop(**_kwargs):
    from app.admin.telemetry import flush_events
    flush_events()


@worker_ready.connect
def _operations_start(sender=None, **_kwargs):
    import json
    import threading
    from app.admin.settings import redis_client
    from app.admin.telemetry import configure_telemetry
    configure_telemetry()
    queues = getattr(getattr(sender, "task_consumer", None), "queues", ())
    if CELERY_QUEUE_NAME not in {queue.name for queue in queues}:
        return
    pool = getattr(sender, "pool", None)
    pool_name = type(pool).__module__
    capacity = 1 if "solo" in pool_name else int(getattr(pool, "limit", 0) or 0)
    hostname = str(getattr(sender, "hostname", "worker"))
    def heartbeat():
        while True:
            try:
                redis_client().set("ql:runtime:worker:" + hostname,
                    json.dumps({"hostname": hostname, "pool": pool_name, "capacity": capacity}), ex=35)
            except Exception:
                pass
            threading.Event().wait(10)
    threading.Thread(target=heartbeat, name="runtime-capacity", daemon=True).start()


# ── Qdrant bootstrap — on worker startup, NOT on import ───────────────────────
@worker_ready.connect
def _bootstrap_qdrant(sender=None, **_kwargs):
    """
    Ensure the Qdrant collection + payload indexes exist, once, when a worker
    comes up. Guarded: a Qdrant outage logs a warning but never prevents the
    worker from starting (embed tasks retry, and ensure_collection is idempotent
    so it can run again on the next worker boot).
    """
    queues = getattr(getattr(sender, "task_consumer", None), "queues", ())
    if queues and settings.CELERY_EMBED_QUEUE not in {queue.name for queue in queues}:
        return
    try:
        from app.vectorstore.qdrant_store import ensure_collection
        ensure_collection()
        logger.info("Qdrant collection ensured on worker startup.")
    except Exception as e:
        logger.warning(
            f"Qdrant ensure_collection failed on startup (RAG embeds will retry): {e}"
        )
