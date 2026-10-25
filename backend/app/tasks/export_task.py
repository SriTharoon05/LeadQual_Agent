"""Disk-based exports isolated from qualification/embedding worker queues."""
import logging
import errno
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from uuid import uuid4
from celery.signals import worker_process_init, worker_ready, worker_shutdown

from app.celery_app import celery
from app.config import settings
from app.database import SessionLocal, engine
from app.export.result_builder import _upload_file, write_export
from app.export.service import (LEASE_SECONDS, WORKERS_KEY, active_key, cache_key, get_state,
    lease_key, release_key, source_revision, store, update_owned)
from app.models import Job

logger = logging.getLogger(__name__)
_worker_stop = threading.Event()
_worker_name = None


@worker_process_init.connect
def reset_export_database_pool(**_kwargs):
    # Prefork children must not use database connections opened by their parent.
    engine.dispose(close=False)


@worker_ready.connect
def advertise_export_worker(sender=None, **_kwargs):
    global _worker_name
    queues = getattr(getattr(sender, "task_consumer", None), "queues", ())
    if settings.CELERY_EXPORT_QUEUE not in {queue.name for queue in queues}:
        return
    _worker_name = str(getattr(sender, "hostname", uuid4()))
    _worker_stop.clear()

    def advertise():
        # Remove only this exporter's abandoned scratch directories, after an
        # age greater than any allowed task lifetime. Never follow symlinks.
        root = Path(gettempdir()).resolve()
        for path in root.glob("ql-export-*"):
            try:
                if (not path.is_symlink() and path.is_dir() and path.resolve().parent == root
                        and time.time() - path.stat().st_mtime > 86_400):
                    shutil.rmtree(path)
            except OSError:
                logger.warning("Could not clean abandoned export scratch directory")
        while not _worker_stop.is_set():
            try:
                store().zadd(WORKERS_KEY, {_worker_name: time.time()})
                store().expire(WORKERS_KEY, 180)
            except Exception:
                logger.warning("Could not advertise export worker availability")
            if _worker_stop.wait(20):
                return
    threading.Thread(target=advertise, name="export-worker-heartbeat", daemon=True).start()


@worker_shutdown.connect
def stop_export_advertisement(**_kwargs):
    _worker_stop.set()
    if _worker_name:
        try:
            store().zrem(WORKERS_KEY, _worker_name)
        except Exception:
            pass


@celery.task(bind=True, max_retries=2, ignore_result=True,
    time_limit=settings.EXPORT_TIMEOUT_SECONDS + 60)
def build_export_file(self, export_id):
    state = get_state(export_id)
    if not state or state["status"] in ("ready", "failed"):
        return
    token = str(uuid4())
    if not store().set(lease_key(export_id), token, nx=True, ex=LEASE_SECONDS):
        return
    state = get_state(export_id)
    if not state or state["status"] in ("ready", "failed"):
        release_key(lease_key(export_id), token)
        return
    stopped = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                update_owned(export_id, token)
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    blob_name = None
    try:
        attempt = int(state.get("attempts", 0)) + 1
        if attempt > 3:
            raise ValueError("The export could not finish after three attempts. Please retry.")
        deadline = state["created_at"] + settings.EXPORT_TIMEOUT_SECONDS

        def check_time():
            if lost.is_set():
                raise RuntimeError("Export worker lost its lease")
            if time.time() > deadline:
                raise ValueError("Export took too long. Please retry or ask an administrator to check the export worker.")

        def progress(count):
            check_time()
            update_owned(export_id, token, rows_written=count, phase="Writing lead records")

        update_owned(export_id, token, status="running", phase="Loading saved lead records",
                     attempts=attempt, rows_written=0, error=None)
        with SessionLocal() as db:
            job = db.get(Job, state["job_id"])
            if not job or job.status != "completed":
                raise ValueError("The upload is no longer available for export.")
            revision, total = source_revision(db, job)
        as_of = datetime.now(timezone.utc)
        extension = "xlsx" if state["format"] == "excel" else "csv"
        content_type = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if extension == "xlsx" else "text/csv; charset=utf-8")
        update_owned(export_id, token, total_rows=total)
        with TemporaryDirectory(prefix="ql-export-") as directory:
            path = Path(directory) / f"results.{extension}"
            count = write_export(state["job_id"], state["format"], path, as_of, progress)
            check_time()
            with SessionLocal() as db:
                job = db.get(Job, state["job_id"])
                if not job or job.status != "completed":
                    raise ValueError("The upload was removed while exporting.")
                after, after_count = source_revision(db, job)
            if revision != after or count != total or count != after_count:
                raise RuntimeError("Lead records changed while exporting; rebuilding a current snapshot")
            update_owned(export_id, token, rows_written=count, phase="Uploading download file")
            # Attempt-specific names fence crashes/retries even at the Blob layer.
            blob_name = f"exports/{state['job_id']}/prepared/{export_id}/{token}.{extension}"
            _upload_file(path, blob_name, content_type)
            check_time()
            with SessionLocal() as db:
                job = db.get(Job, state["job_id"])
                if job is None:
                    raise ValueError("The upload was removed while exporting.")
                uploaded_revision, uploaded_count = source_revision(db, job)
                if uploaded_revision != revision or uploaded_count != count:
                    raise RuntimeError("Lead records changed during upload; rebuilding a current snapshot")
            index = cache_key(state["job_id"], state["format"], revision)
            update_owned(export_id, token, status="ready", phase="Ready to download",
                blob_name=blob_name, content_type=content_type, size_bytes=path.stat().st_size,
                ready_at=time.time(), filename=f"job_{state['job_id']}.{extension}",
                snapshot_at=as_of.isoformat(), revision=revision, cache_key=index)
        # A completed file stays valid if optional cache registration fails.
        try:
            if state["cache_key"] != index:
                release_key(state["cache_key"], export_id)
            store().eval("""
                local current=redis.call('GET',KEYS[1])
                if not current or current == ARGV[1] then
                    redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]); return 1 end
                return 0
            """, 1, index, export_id, settings.EXPORT_CACHE_SECONDS)
            release_key(active_key(state["job_id"], state["format"]), export_id)
        except Exception:
            logger.warning("Export %s is ready; optional cache registration failed", export_id)
    except Exception as exc:
        logger.exception("Export %s failed (%s)", export_id, type(exc).__name__)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            exc = ValueError("Not enough temporary disk space. Ask an administrator to increase export worker scratch storage, then retry.")
        if blob_name:
            try:
                from app.export.service import blob_service
                with blob_service() as service:
                    service.get_blob_client(settings.AZURE_BLOB_CONTAINER, blob_name).delete_blob()
            except Exception:
                logger.warning("Could not clean incomplete export %s", export_id)
        try:
            retry = not isinstance(exc, ValueError) and int(state.get("attempts", 0)) < 2 and not lost.is_set()
            update_owned(export_id, token, status="queued" if retry else "failed",
                phase="Retrying export" if retry else "Export failed",
                error=None if retry else (str(exc) if isinstance(exc, ValueError)
                    else "Could not prepare the export. Please retry; if it persists, ask an administrator to check export logs."))
            if not retry:
                release_key(state["cache_key"], export_id)
        except Exception:
            retry = False
            logger.warning("Could not update failed export state %s", export_id)
        if retry:
            raise self.retry(exc=exc, countdown=5)
    finally:
        stopped.set()
        thread.join(timeout=3)
        try:
            release_key(lease_key(export_id), token)
        except Exception:
            logger.warning("Could not release export lease %s", export_id)
