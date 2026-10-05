"""
celery_app.py
─────────────
Celery application singleton.  (HARDENED, production-grade edition)

Key production fixes vs. the original:
  • ensure_collection() no longer runs at IMPORT time. Running it on import
    meant that if Qdrant Cloud was briefly unreachable, importing this module
    (which the API and EVERY worker do) would raise and take the whole app
    down. It now runs on the worker-ready signal, wrapped so a Qdrant outage
    degrades gracefully (RAG embed simply retries later) instead of crashing.
  • worker_concurrency is set from settings.MAX_WORKERS, so the worker count
    lives in .env and you never have to remember the --concurrency flag.
  • Startup side-effects are guarded.

Usage in tasks:
    from app.celery_app import celery

Start a worker (concurrency comes from MAX_WORKERS in .env):
    celery -A app.celery_app.celery worker --loglevel=info -Q lead_processing

    # Override concurrency ad-hoc if needed:
    celery -A app.celery_app.celery worker -Q lead_processing --concurrency=50
"""

import logging

from celery import Celery
from celery.signals import worker_ready

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
             "app.tasks.refresh_task"],
)

# Concurrency from .env (MAX_WORKERS). CLI --concurrency still overrides this.
try:
    _concurrency = int(getattr(settings, "MAX_WORKERS", 10) or 10)
except Exception:
    _concurrency = 10

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
    },

    # Worker behaviour — threads pool suits this I/O-bound pipeline.
    worker_pool="threads",
    worker_concurrency=_concurrency,     # ← from MAX_WORKERS (.env)
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


# ── Qdrant bootstrap — on worker startup, NOT on import ───────────────────────
@worker_ready.connect
def _bootstrap_qdrant(**_kwargs):
    """
    Ensure the Qdrant collection + payload indexes exist, once, when a worker
    comes up. Guarded: a Qdrant outage logs a warning but never prevents the
    worker from starting (embed tasks retry, and ensure_collection is idempotent
    so it can run again on the next worker boot).
    """
    try:
        from app.vectorstore.qdrant_store import ensure_collection
        ensure_collection()
        logger.info("Qdrant collection ensured on worker startup.")
    except Exception as e:
        logger.warning(
            f"Qdrant ensure_collection failed on startup (RAG embeds will retry): {e}"
        )
