"""Short preparation/status requests and authenticated, resumable downloads."""
import logging
import re
from typing import Literal
from uuid import UUID

import jwt
from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.export.service import (ExportWorkerUnavailable, blob_service, ensure_dispatched, get_state,
    public_status, request_export, verify_ticket)
from app.models import Job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs/{job_id}", tags=["Exports"])
NO_CACHE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class DownloadTicketLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and "/exports/" in path and "/file" in path and "ticket=" in path:
                args = list(record.args)
                args[2] = path.partition("?")[0] + "?ticket=[redacted]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(DownloadTicketLogFilter())


def _job(db, job_id):
    job = db.get(Job, str(job_id))
    if job is None:
        raise HTTPException(404, "Upload not found.")
    if job.status != "completed":
        raise HTTPException(409, "Wait for this upload to finish before exporting.")
    return job


def _state(job_id, export_id):
    state = get_state(str(export_id))
    if state is None or state["job_id"] != str(job_id):
        raise HTTPException(404, "This export expired. Create a new export.")
    return state


@router.post("/exports", status_code=202)
def prepare_export(job_id: UUID, format: Literal["excel", "csv"] = Query(...),
                   db: Session = Depends(get_db)):
    job = _job(db, job_id)
    try:
        state = request_export(db, job, format)
        result = public_status(state)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ExportWorkerUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not schedule %s export for %s", format, job_id)
        raise HTTPException(503, "Export service is temporarily unavailable. Please retry shortly.") from exc
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 202, headers=NO_CACHE)


@router.get("/exports/{export_id}")
def export_status(job_id: UUID, export_id: UUID, db: Session = Depends(get_db)):
    _job(db, job_id)
    try:
        state = _state(job_id, export_id)
        ensure_dispatched(state)
        return JSONResponse(public_status(_state(job_id, export_id)), headers=NO_CACHE)
    except HTTPException:
        raise
    except ExportWorkerUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not read export %s", export_id)
        raise HTTPException(503, "Could not check export progress. Retrying will resume this export.") from exc


def _byte_range(value, size):
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or (not match[1] and not match[2]):
        raise HTTPException(416, "Unsupported byte range.", headers={"Content-Range": f"bytes */{size}"})
    if not match[1]:
        length = int(match[2])
        start, end = max(0, size - length), size - 1
        if not length:
            start = size
    else:
        start = int(match[1])
        end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start > end or start >= size:
        raise HTTPException(416, "The requested range is outside this file.", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True


@router.get("/exports/{export_id}/file")
def download_file(job_id: UUID, export_id: UUID, request: Request,
                  ticket: str = Query(..., max_length=2048), db: Session = Depends(get_db)):
    # Authentication middleware only permits this exact GET without a bearer.
    # Verify the separate download audience/purpose/path before any file access.
    try:
        verify_ticket(ticket, str(job_id), str(export_id))
    except jwt.PyJWTError as exc:
        raise HTTPException(403, "This download link expired or is invalid. Request a fresh link.") from exc
    _job(db, job_id)
    service = None
    try:
        state = _state(job_id, export_id)
        if state["status"] != "ready":
            raise HTTPException(409, "This export is not ready to download.")
        service = blob_service()
        blob = service.get_blob_client(settings.AZURE_BLOB_CONTAINER, state["blob_name"])
        properties = blob.get_blob_properties()
        start, end, partial = _byte_range(request.headers.get("range"), properties.size)
        download = blob.download_blob(offset=start, length=end - start + 1, max_concurrency=1)
    except ResourceNotFoundError as exc:
        if service:
            service.close()
        raise HTTPException(410, "The export file expired. Create a new export.") from exc
    except HTTPException:
        if service:
            service.close()
        raise
    except Exception as exc:
        if service:
            service.close()
        logger.exception("Could not open export %s", export_id)
        raise HTTPException(503, "The download could not start. Please retry the download link.") from exc
    # Release the SQL connection before a potentially long browser transfer.
    db.close()

    def chunks():
        try:
            yield from download.chunks()
        finally:
            service.close()

    headers = {**NO_CACHE, "Content-Disposition": f'attachment; filename="{state["filename"]}"',
        "Content-Length": str(end - start + 1), "Accept-Ranges": "bytes",
        "ETag": str(properties.etag), "X-Accel-Buffering": "no"}
    # The byte counts/ranges refer to the stored file, not a gzip variant.
    headers["Content-Encoding"] = "identity"
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{properties.size}"
    return StreamingResponse(chunks(), status_code=206 if partial else 200,
        media_type=state["content_type"], headers=headers, background=BackgroundTask(service.close))


@router.get("/export/{format}", include_in_schema=False)
def legacy_export(job_id: UUID, format: Literal["excel", "csv"], db: Session = Depends(get_db)):
    """Legacy URLs now prepare asynchronously; ready requests redirect to a file."""
    response = prepare_export(job_id, format, db)
    import json
    result = json.loads(response.body)
    if result.get("download_path"):
        return RedirectResponse(result["download_path"], status_code=303, headers=NO_CACHE)
    return response
