"""
main.py
───────
FastAPI application — all HTTP routes.

Endpoints:
  POST   /auth/login                  Static admin login
  POST   /upload                      Upload Excel or CSV, create job, store to Blob
  POST   /jobs/{job_id}/icp           Submit ICP prompt and dispatch Celery tasks
  GET    /jobs                        List all jobs
  GET    /jobs/{job_id}/status        Poll progress
  GET    /jobs/{job_id}/results       All lead results for a job (PAGINATED)
  GET    /jobs/{job_id}/results/{id}  Single lead result
  POST   /jobs/{job_id}/exports       Prepare all saved rows as Excel or CSV
  GET    /jobs/{job_id}/exports/{id}  Export progress / signed download path
  DELETE /jobs/{job_id}               Delete job and all data
  GET    /health                      Health check

Start with:
    uvicorn app.main:app --reload --port 8000
"""

import logging
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

from azure.storage.blob import BlobServiceClient
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import BigInteger, cast, case, event, func, or_, update
from sqlalchemy.orm import Session, joinedload, selectinload, load_only, noload
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError

from app.config import settings, SUPPORTED_EXTENSIONS
from app.database import get_db, ping_db, schema_ready
from app.utils.db_availability import database_unavailable_reason, warn_database_unavailable
from app.admin.telemetry import configure_telemetry, set_context, clear_context, publish_event
from app.models import (Job, Lead, FinalScore, AdminUser, LeadTracking, LeadRefresh,
                        ProcessingLog, LLMCallLog, ValidationResult, ChatConversation, ChatMessage)
from app.lead_results import LeadResultResponse, latest_refreshes, lead_to_response as _lead_to_response
from app.admin.routes import router as admin_router
from app.lead_filters import LeadFilters, filtered_query
from app.refresh import router as refresh_router
from app.utils.lead_identity import transaction_lock
from app.export.routes import router as export_router
from app.tasks.dispatch_task import request_dispatch
from app.chat.routes import router as chat_router
from app.vectorstore.qdrant_store import delete_job as qdrant_delete_job
from app.auth import AuthenticationMiddleware, create_access_token, verify_password
from app.utils.chat_lease import ConversationLease


logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AI Lead Qualification API",
    description="Ingest, validate, enrich, and score B2B sales leads.",
    version="1.0.0",
)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(refresh_router)
app.include_router(export_router)

app.add_middleware(AuthenticationMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges", "ETag"],
)


@app.exception_handler(DBAPIError)
@app.exception_handler(SQLAlchemyTimeoutError)
async def database_error_handler(request: Request, exc: DBAPIError | SQLAlchemyTimeoutError):
    # Driver messages may contain infrastructure details; keep them in server logs.
    reason = database_unavailable_reason(exc)
    if reason:
        warn_database_unavailable(logger, "API database request", exc)
        return JSONResponse(status_code=503, headers={"Retry-After": "5"}, content={
            "detail": "Database unavailable. Check database connectivity and Azure SQL firewall access."
        })
    logger.error("Database request failed", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Database request failed."})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    clear_context()
    parts = request.url.path.split("/")
    if len(parts) > 2 and parts[1] == "jobs":
        try:
            set_context(job_id=str(UUID(parts[2])))
        except ValueError:
            pass
    try:
        response = await call_next(request)
    except Exception as exc:
        publish_event(f"API request failed ({type(exc).__name__}). Check server diagnostics and retry after the service recovers.",
                      severity="ERROR", kind="request_error", component="api")
        raise
    finally:
        clear_context()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.APP_ENV.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.on_event("startup")
def start_operations_telemetry():
    configure_telemetry()


@app.on_event("shutdown")
def flush_operations_telemetry():
    from app.admin.telemetry import flush_events
    flush_events()


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class ICPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    icp_prompt: str = Field(min_length=1, max_length=8000)
    processing_mode: Literal["reuse", "fresh"] = "reuse"


class JobStatusResponse(BaseModel):
    processing_mode: Literal["reuse", "fresh"] = "reuse"
    job_id:         str
    status:         str
    file_name:      Optional[str] = None      # (fixed: no trailing comma → not a tuple)
    file_type:      Optional[str] = None
    total_rows:     Optional[int] = None
    processed_rows: int
    failed_rows:    int = 0
    reused_rows: int = 0
    research_reused_rows: int = 0
    icp_prompt: Optional[str] = None
    progress_pct:   float
    duplicates_removed: Optional[int] = None   # ← ADD
    import_error:       Optional[str] = None   # ← ADD
    upload_pending: bool = False
    created_at:     datetime
    completed_at:   Optional[datetime] = None
    llm_model:         Optional[str] = None
    embed_model:       Optional[str] = None
    total_tokens_used: Optional[int] = None
    prompt_tokens:     Optional[int] = None
    completion_tokens: Optional[int] = None


class PaginatedResults(BaseModel):
    """Wrapper for paginated /results — gives the frontend total + page info."""
    items:       list[LeadResultResponse]
    total:       int          # total leads for this job
    limit:       int          # page size used
    offset:      int          # starting index of this page
    returned:    int          # number of items in this page
    has_more:    bool         # whether more pages exist after this one


class TrackingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    reviewed: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=10000)


class UploadResponse(BaseModel):
    job_id:     str
    file_name:  str
    file_type:  str
    status:     str
    message:    str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    token:    str
    username: str


# ── POST /auth/login ──────────────────────────────────────────────────────────
@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    Admin login. Looks up the username, verifies its bcrypt password, and
    returns an expiring signed token sent by the frontend as a Bearer header.
    """
    username = (body.username or "").strip()
    if not username or not body.password:
        raise HTTPException(status_code=422, detail="Username and password are required.")
    if not _login_allowed(request, username):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in one minute.")

    user = db.query(AdminUser).filter(AdminUser.username == username).first()

    # Constant-time-ish comparison; generic error message either way.
    if not user or not verify_password(body.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    token = create_access_token(user.username)

    logger.info(f"Admin login successful: {username}")
    return LoginResponse(token=token, username=user.username)


# ── POST /upload ──────────────────────────────────────────────────────────────

@app.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
def upload_file(
    file: UploadFile = File(...),
    request_id: UUID | None = Form(default=None),
    db:   Session    = Depends(get_db),
):
    """
    Store an Excel/CSV upload; parsing and qualification run asynchronously.

    Heavy work (parse + LLM column-mapping + dedup + bulk insert) runs in a
    background Celery task. A stable request ID recovers an accepted transfer
    after a browser timeout instead of creating a duplicate job.
    The frontend polls GET /jobs/{job_id}/status:
        importing → queued (ready for ICP)  |  import_failed (see import_error)
    """
    # ── Validate extension ────────────────────────────────────────────────────
    suffix = _file_suffix(file.filename)
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {SUPPORTED_EXTENSIONS}",
        )

    # ── Read + size-check ─────────────────────────────────────────────────────
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    # UploadFile is already a seekable spooled file. Inspect its length and
    # stream to Blob instead of making another complete 50 MB memory copy.
    file.file.seek(0, 2)
    size_bytes = file.file.tell()
    file.file.seek(0)
    size_mb = size_bytes / (1024 * 1024)
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max: {settings.MAX_UPLOAD_SIZE_MB} MB",
        )
    if not size_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(file.filename or "") > 500:
        raise HTTPException(status_code=422, detail="The file name is too long.")

    signature = hashlib.sha256()
    signature.update((str(file.filename or "") + "\0" + suffix + "\0").encode("utf-8"))
    while block := file.file.read(256 * 1024):
        signature.update(block)
    file.file.seek(0)
    fingerprint = signature.hexdigest()
    job_id = str(request_id or uuid4())
    lease = ConversationLease("upload:" + job_id)
    guarded = False
    try:
        lease.acquire()
        event.listen(db, "before_commit", lease.assert_owned)
        guarded = True
        job = db.get(Job, job_id)
        if job is not None:
            if not job.upload_fingerprint or job.upload_fingerprint != fingerprint:
                raise HTTPException(409, "This upload request ID was used for a different file. Select a new file/upload request.")
            if job.source_blob_name is not None:
                return UploadResponse(job_id=job_id, file_name=job.file_name or file.filename,
                    file_type=job.file_type or suffix.lstrip("."), status=job.status,
                    message="This upload was already accepted. Continue with the existing job.")
            job.status, job.import_error = "importing", None
        else:
            job = Job(id=job_id, status="importing", total_rows=None,
                file_name=file.filename, file_type=suffix.lstrip("."), upload_fingerprint=fingerprint)
            db.add(job)
        # A NULL source is an unfinished transfer, never an importable outbox.
        job.source_blob_name = None
        job.dispatch_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        blob_name = f"uploads/{job_id}/source{suffix}"
        if not _store_raw_to_blob(file.file, blob_name):
            job.import_error = "The file transfer was interrupted. Retry the same file and request ID."
            db.commit()
            raise HTTPException(503, "File storage unavailable. Retry the same file to resume this upload.")
        lease.assert_owned()
        job.source_blob_name = blob_name
        job.dispatch_updated_at = None
        job.import_error = None
        db.commit()
        try:
            request_dispatch(job_id, force=True)
        except Exception:
            logger.exception("Import publication deferred for job %s; SQL intent retained", job_id)
        return UploadResponse(job_id=job_id, file_name=file.filename, file_type=suffix.lstrip("."),
            status="importing", message="File received. Import is queued; poll the existing job for progress.")
    except HTTPException:
        raise
    except TimeoutError as exc:
        db.rollback()
        raise HTTPException(503, "This upload is still transferring or ownership was interrupted. Retry the same file/request ID.",
                            headers={"Retry-After": "3"}) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Upload request %s could not finish", job_id)
        raise HTTPException(503, "Upload service is temporarily unavailable. Retry with the same request ID.",
                            headers={"Retry-After": "3"}) from exc
    finally:
        try:
            if guarded:
                event.remove(db, "before_commit", lease.assert_owned)
        finally:
            lease.release()

# ── POST /jobs/{job_id}/icp ───────────────────────────────────────────────────
@app.post("/jobs/{job_id}/icp", status_code=status.HTTP_202_ACCEPTED)
def submit_icp(
    job_id:  UUID,
    body:    ICPRequest,
    db:      Session = Depends(get_db),
):
    """
    Attach an ICP prompt to a job and dispatch all Celery tasks.
    Returns immediately — frontend polls /jobs/{job_id}/status.
    """
    job_id = str(job_id)
    transaction_lock(db, "ql:dispatch:" + job_id)
    job: Job = _get_job_or_404(job_id, db)

    if (job.status == "running" and job.icp_prompt == body.icp_prompt.strip()
            and job.processing_mode == body.processing_mode):
        # Recover an accepted submission after a browser timeout; a different
        # prompt remains forbidden while any published work can still run.
        db.commit()
        try:
            request_dispatch(job_id)
        except Exception:
            logger.warning("Accepted processing will recover for job %s", job_id)
        return {"job_id": job_id, "status": "running", "tasks_queued": job.total_rows or 0,
                "message": "This processing request was already accepted."}
    if job.status not in ("queued",):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already '{job.status}'. The ICP and research mode cannot change after processing starts. Start a new upload for another run.",
        )

    if not body.icp_prompt.strip():
        raise HTTPException(status_code=422, detail="icp_prompt cannot be empty.")

    # Save ICP prompt and mark running
    job.icp_prompt = body.icp_prompt.strip()
    job.processing_mode = body.processing_mode
    job.status     = "running"
    db.flush()

    lead_count = int(db.query(func.count(Lead.id)).filter(Lead.job_id == job_id).scalar() or 0)
    if not lead_count:
        raise HTTPException(status_code=422, detail="Job has no leads to process.")
    job.dispatch_row_index = job.dispatch_lead_id = None
    job.dispatch_complete = False
    job.dispatch_updated_at = None

    # Commit before dispatch so workers can always observe the running state.
    db.commit()
    try:
        request_dispatch(job_id, force=True)
    except Exception:
        # Some messages may already have been published. Never reopen the ICP
        # form or allow a different prompt to race those in-flight tasks.
        logger.exception("Processing publication deferred for job %s; SQL intent retained", job_id)

    logger.info("Job %s accepted for paged dispatch of %s leads", job_id, lead_count)

    return {
        "job_id":      job_id,
        "status":      "running",
        "tasks_queued": lead_count,
        "message":     f"Accepted {lead_count} leads for processing. Poll /jobs/{job_id}/status for progress.",
    }


# ── GET /jobs ─────────────────────────────────────────────────────────────────
@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    """List all jobs, newest first."""
    jobs = db.query(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(100).all()
    return [_job_to_status(j) for j in jobs]


@app.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    """Return compact aggregates without transferring every lead to the UI."""
    job_row = db.query(
        func.count(Job.id).label("total"),
        func.sum(case((Job.status == "completed", 1), else_=0)).label("completed"),
        func.sum(case((Job.status.in_(("importing", "queued", "running")), 1), else_=0)).label("active"),
        func.sum(case((Job.status.in_(("failed", "import_failed")), 1), else_=0)).label("failed"),
    ).one()
    lead_row = (
        db.query(
            func.count(FinalScore.id).label("total"),
            func.sum(case((FinalScore.priority == "High", 1), else_=0)).label("high"),
            func.sum(case((FinalScore.priority == "Medium", 1), else_=0)).label("medium"),
            func.sum(case((FinalScore.priority == "Low", 1), else_=0)).label("low"),
            func.avg(FinalScore.total_score).label("average"),
        )
        .join(Lead, Lead.id == FinalScore.lead_id)
        .join(Job, Job.id == Lead.job_id)
        .filter(Job.status == "completed")
        .one()
    )
    return {
        "total_jobs": int(job_row.total or 0),
        "completed_jobs": int(job_row.completed or 0),
        "active_jobs": int(job_row.active or 0),
        "failed_jobs": int(job_row.failed or 0),
        "total_leads": int(lead_row.total or 0),
        "high_priority": int(lead_row.high or 0),
        "medium_priority": int(lead_row.medium or 0),
        "low_priority": int(lead_row.low or 0),
        "avg_final_score": round(float(lead_row.average or 0), 1),
    }


# ── GET /jobs/{job_id}/status ─────────────────────────────────────────────────
@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def job_status(job_id: str, db: Session = Depends(get_db)):
    job = _get_job_or_404(job_id, db)
    if (job.status == "importing" and job.source_blob_name or job.status == "running" and
            (not job.dispatch_complete or job.processed_rows >= (job.total_rows or 0))):
        db.commit()
        try:
            request_dispatch(str(job.id))
        except Exception:
            logger.warning("Job recovery will retry for %s", job_id)
        db.refresh(job)
    return _job_to_status(job)


# ── GET /jobs/{job_id}/results ────────────────────────────────────────────────
@app.get("/jobs/{job_id}/results", response_model=PaginatedResults)
def job_results(
    job_id: str,
    details: bool = Query(True),
    limit:  int = Query(100, ge=1, le=500, description="Page size (max 500)."),
    offset: int = Query(0,  ge=0,          description="Rows to skip (page offset)."),
    search: Optional[str] = Query(None, max_length=100),
    priority: Optional[Literal["High", "Medium", "Low"]] = None,
    reviewed: bool | None = None,
    country: str | None = Query(None, max_length=100),
    industry: str | None = Query(None, max_length=100),
    min_score: float | None = Query(None, ge=0, le=100),
    evidence: Literal["all", "fresh", "stale", "unknown"] = "all",
    sort_by: Literal["row_index", "company_name", "contact_name", "email_address", "final_score", "priority"] = "row_index",
    sort_dir: Literal["asc", "desc"] = "asc",
    db:     Session = Depends(get_db),
):
    """
    Return processed lead results for a job — PAGINATED.

    Performance: uses joinedload(final_score) + selectinload(validation_results)
    to eager-load relationships in ~2 queries instead of the old N+1 explosion
    (which fired ~1,000 queries for 500 leads and made the page hang).

    Response is a PaginatedResults wrapper:
        { items: [...], total, limit, offset, returned, has_more }
    """
    _get_job_or_404(job_id, db)

    try:
        filters = LeadFilters(search=search or "", priority=priority, reviewed=reviewed,
                              country=country or "", industry=industry or "",
                              min_score=min_score, evidence=evidence)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid lead filters.") from exc
    base = filtered_query(db, filters, datetime.now(timezone.utc)).filter(Lead.job_id == job_id)

    # Total count for the whole job (cheap COUNT query, no row hydration).
    total = base.with_entities(func.count(Lead.id)).scalar() or 0

    sort_columns = {
        "row_index": Lead.row_index,
        "company_name": Lead.company_name,
        "contact_name": Lead.contact_name,
        "email_address": Lead.email_address,
        "final_score": FinalScore.total_score,
        "priority": case((FinalScore.priority == "High", 3), (FinalScore.priority == "Medium", 2), (FinalScore.priority == "Low", 1), else_=0),
    }
    order_column = sort_columns[sort_by]
    order = order_column.desc() if sort_dir == "desc" else order_column.asc()
    ordering = [order]
    if sort_by == "priority":
        ordering.append(FinalScore.total_score.desc())
    if sort_by != "row_index":
        ordering.append(Lead.row_index.asc())
    ordering.append(Lead.id.asc())
    score_loader = joinedload(Lead.final_score)
    if not details:
        score_loader = score_loader.load_only(FinalScore.total_score, FinalScore.icp_score,
            FinalScore.growth_score, FinalScore.contact_score, FinalScore.priority,
            FinalScore.qualification_status, FinalScore.scoring_version)
    summary_load = [] if details else [load_only(Lead.id, Lead.job_id, Lead.row_index,
        Lead.contact_name, Lead.company_name, Lead.job_title, Lead.email_address,
        Lead.reuse_kind, Lead.reused_from_lead_id, Lead.evidence_checked_at, Lead.tracking_id)]
    leads = (
        base
        .options(
            joinedload(Lead.job).load_only(Job.icp_prompt) if details else noload(Lead.job),
            score_loader, noload(Lead.refresh_requests),
            selectinload(Lead.validation_results) if details else noload(Lead.validation_results),
            *summary_load,
        )
        .order_by(*ordering)
        .offset(offset)
        .limit(limit)
        .all()
    )

    latest = latest_refreshes(db, [lead.id for lead in leads])
    items = [_lead_to_response(lead, include_details=details,
        latest_refresh=latest.get(str(lead.id))) for lead in leads]

    return PaginatedResults(
        items=    items,
        total=    int(total),
        limit=    limit,
        offset=   offset,
        returned= len(items),
        has_more= (offset + len(items)) < int(total),
    )


# ── GET /jobs/{job_id}/results/{lead_id} ──────────────────────────────────────
@app.get("/jobs/{job_id}/results/{lead_id}", response_model=LeadResultResponse)
def lead_result(job_id: str, lead_id: str, db: Session = Depends(get_db)):
    """Return a single lead result."""
    _get_job_or_404(job_id, db)
    lead = (
        db.query(Lead)
        .filter(Lead.id == lead_id, Lead.job_id == job_id)
        .options(
            joinedload(Lead.job), joinedload(Lead.final_score),
            selectinload(Lead.validation_results), noload(Lead.refresh_requests),
        )
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return _lead_to_response(lead, latest_refresh=latest_refreshes(db, [lead.id]).get(str(lead.id)))


# ── GET /jobs/{job_id}/export/excel ──────────────────────────────────────────
@app.patch("/jobs/{job_id}/results/{lead_id}/tracking", response_model=LeadResultResponse)
def update_tracking(job_id: str, lead_id: str, body: TrackingUpdate,
                    db: Session = Depends(get_db)):
    lead = db.query(Lead).options(noload(Lead.refresh_requests)).filter(Lead.id == lead_id, Lead.job_id == job_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.tracking_id:
        raise HTTPException(status_code=409, detail="Lead tracking requires the database migration")
    changes = {}
    for field, column in (("reviewed", "reviewed"), ("notes", "notes")):
        if field in body.model_fields_set:
            value = getattr(body, field)
            if value is None:
                raise HTTPException(status_code=422, detail=f"{field} cannot be null")
            changes[column] = value
    if not changes:
        raise HTTPException(status_code=422, detail="Provide a reviewed or notes change.")
    changes.update(version=LeadTracking.version + 1, updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
    result = db.execute(update(LeadTracking).where(
        LeadTracking.id == lead.tracking_id, LeadTracking.version == body.version
    ).values(**changes).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Tracking changed elsewhere. Reload the lead and try again.")
    db.commit()
    db.expire_all()
    return _lead_to_response(lead, latest_refresh=latest_refreshes(db, [lead.id]).get(str(lead.id)))


from app.models import CompanyScrape

@app.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job_id = str(job_id)
    transaction_lock(db, "ql:dispatch:" + job_id)
    job = _get_job_or_404(job_id, db)
    upload_lease = None
    if job.status == "importing" and not job.source_blob_name:
        upload_lease = ConversationLease("upload:" + job_id)
        try:
            upload_lease.acquire()
        except Exception as exc:
            raise HTTPException(409, "The file is still transferring. Wait for it to finish before deleting.") from exc
    elif job.status in ("running", "importing"):
        raise HTTPException(409, "Wait for this upload to finish before deleting it.")
    try:
        try:
            transaction_lock(db, "ql:artifacts:" + job_id, timeout_ms=0)
        except RuntimeError as exc:
            raise HTTPException(409, "Wait for active website indexing to finish before deleting this upload.") from exc
        if db.query(LeadRefresh.id).join(Lead, Lead.id == LeadRefresh.lead_id).filter(
                Lead.job_id == job_id, LeadRefresh.status.in_(("queued", "running"))).first():
            raise HTTPException(409, "Wait for the active evidence refresh to finish before deleting this upload.")
        transaction_lock(db, "ql:lead-import")
        tracking_ids = [row[0] for row in db.query(Lead.tracking_id).filter(
            Lead.job_id == job_id, Lead.tracking_id.isnot(None)).distinct().all()]
        lead_ids = db.query(Lead.id).filter(Lead.job_id == job_id)
        # Explicit set-based deletes avoid loading every lead, score, validation,
        # refresh and log just to perform ORM cascades on a large upload.
        for model in (ProcessingLog, LLMCallLog):
            db.query(model).filter(or_(model.job_id == job_id, model.lead_id.in_(lead_ids))).delete(synchronize_session=False)
        for model in (CompanyScrape, ValidationResult, FinalScore, LeadRefresh):
            db.query(model).filter(model.lead_id.in_(lead_ids)).delete(synchronize_session=False)
        conversations = db.query(ChatConversation.id).filter(ChatConversation.job_id == job_id)
        db.query(ChatMessage).filter(or_(ChatMessage.job_id == job_id,
            ChatMessage.conversation_id.in_(conversations))).delete(synchronize_session=False)
        db.query(ChatConversation).filter(ChatConversation.job_id == job_id).delete(synchronize_session=False)
        db.query(Lead).filter(Lead.job_id == job_id).delete(synchronize_session=False)
        db.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
        for start in range(0, len(tracking_ids), 500):
            db.query(LeadTracking).filter(LeadTracking.id.in_(tracking_ids[start:start + 500]),
                ~db.query(Lead.id).filter(Lead.tracking_id == LeadTracking.id).exists()
            ).delete(synchronize_session=False)
        if upload_lease:
            upload_lease.assert_owned()
        db.commit()
    finally:
        if upload_lease:
            upload_lease.release()
    # Never remove files/vectors before the SQL deletion commits successfully.
    # Best-effort remote cleanup also must not hold locks or delay the response.
    background_tasks.add_task(_cleanup_deleted_job_artifacts, job_id)
    logger.info("Job %s deleted", job_id)


def _cleanup_deleted_job_artifacts(job_id: str):
    try:
        qdrant_delete_job(job_id)
    except Exception:
        logger.warning("Vector cleanup failed for deleted job %s", job_id)
    _delete_job_blobs(job_id)

@app.get("/admin/jobs/{job_id}/token-report")
def token_report(job_id: str, db: Session = Depends(get_db)):
    """Per-stage token consumption breakdown for a job."""
    from sqlalchemy import func as sqlfunc
    from app.models import LLMCallLog
    _get_job_or_404(job_id, db)

    rows = (
        db.query(
            LLMCallLog.stage,
            LLMCallLog.module,
            sqlfunc.sum(cast(LLMCallLog.prompt_tokens, BigInteger)).label("prompt"),
            sqlfunc.sum(cast(LLMCallLog.completion_tokens, BigInteger)).label("completion"),
            sqlfunc.sum(cast(LLMCallLog.total_tokens, BigInteger)).label("total"),
            sqlfunc.count(LLMCallLog.id).label("calls"),
            sqlfunc.avg(LLMCallLog.duration_ms).label("avg_ms"),
        )
        .filter(LLMCallLog.job_id == job_id)
        .group_by(LLMCallLog.stage, LLMCallLog.module)
        .order_by(sqlfunc.sum(cast(LLMCallLog.total_tokens, BigInteger)).desc())
        .all()
    )

    total_tokens = sum(r.total or 0 for r in rows)
    return {
        "job_id": job_id,
        "total_tokens": total_tokens,
        "by_stage": [
            {
                "stage":      r.stage,
                "module":     r.module,
                "calls":      r.calls,
                "prompt":     r.prompt,
                "completion": r.completion,
                "total":      r.total,
                "pct":        round((r.total or 0) / total_tokens * 100, 1) if total_tokens else 0,
                "avg_ms":     int(r.avg_ms or 0),
            }
            for r in rows
        ],
    }


# ── GET /health ───────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    db_ok = ping_db()
    return {
        "status":   "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "env":      settings.APP_ENV,
    }


@app.get("/health/live")
def liveness():
    """Cheap App Service liveness probe; does not depend on external services."""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    """Dependency-aware readiness probe for deployment slots and rollouts."""
    redis_ok = False
    try:
        import redis
        redis_ok = bool(redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3).ping())
    except Exception:
        logger.exception("Redis readiness check failed")
    db_ok = ping_db()
    schema_ok = schema_ready() if db_ok else False
    if not (db_ok and redis_ok and schema_ok):
        raise HTTPException(status_code=503, detail={"database": db_ok, "redis": redis_ok, "schema": schema_ok})
    return {"status": "ready", "database": "ok", "redis": "ok", "schema": "ok"}


# ── Internal helpers ──────────────────────────────────────────────────────────
def _get_job_or_404(job_id: str, db: Session) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


def _require_completed(job: Job) -> None:
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Job is '{job.status}'. Exports are only available for completed jobs.",
        )


def _job_to_status(job: Job) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=             str(job.id),
        status=             job.status,
        file_name=          job.file_name,
        file_type=          job.file_type,
        total_rows=         job.total_rows,
        processed_rows=     job.processed_rows,
        failed_rows=        getattr(job, "failed_rows", 0) or 0,
        reused_rows=job.reused_rows or 0,
        research_reused_rows=job.research_reused_rows or 0,
        icp_prompt=job.icp_prompt,
        processing_mode=job.processing_mode,
        progress_pct=       job.progress_pct,
        duplicates_removed= getattr(job, "duplicates_removed", None),
        import_error=       getattr(job, "import_error", None),
        upload_pending=job.status == "importing" and not job.source_blob_name,
        created_at=         _to_ist(job.created_at),        # ← convert
        completed_at=       _to_ist(job.completed_at),      # ← convert
        llm_model=          getattr(job, "llm_model", None),
        embed_model=        getattr(job, "embed_model", None),
        total_tokens_used=  getattr(job, "total_tokens_used", None),
        prompt_tokens=      getattr(job, "prompt_tokens", None),
        completion_tokens=  getattr(job, "completion_tokens", None),
    )


def _file_suffix(filename: Optional[str]) -> str:
    if not filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _upload_source_file(raw: bytes, job_id: str, filename: str, suffix: str) -> None:
    """Store the original uploaded file in Azure Blob (best-effort)."""
    try:
        client    = BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING)
        container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
        blob_name = f"uploads/{job_id}/source{suffix}"
        container.get_blob_client(blob_name).upload_blob(raw, overwrite=True)
    except Exception as e:
        logger.warning(f"Failed to upload source file for job {job_id}: {e}")

def _store_raw_to_blob(raw, blob_name: str) -> bool:
    """Upload raw bytes to Blob at the given name. Returns True on success."""
    try:
        from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
        with BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING,
                connection_timeout=10, read_timeout=60, retry_total=3,
                max_single_put_size=4 * 1024 * 1024, max_block_size=4 * 1024 * 1024) as client:
            container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
            try:
                container.get_container_properties()
            except ResourceNotFoundError:
                try:
                    container.create_container()
                except ResourceExistsError:
                    pass
            container.get_blob_client(blob_name).upload_blob(raw, overwrite=True, max_concurrency=2)
        return True
    except Exception as e:
        logger.error(f"_store_raw_to_blob failed for '{blob_name}': {e}")
        return False


def _delete_job_blobs(job_id: str) -> None:
    """Best-effort cleanup of source, scrape, and export objects for a job."""
    try:
        with BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING,
                connection_timeout=10, read_timeout=60, retry_total=2) as client:
            container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
            for prefix in (f"uploads/{job_id}/", f"scraped/{job_id}/", f"exports/{job_id}/"):
                names = []
                for blob in container.list_blobs(name_starts_with=prefix):
                    names.append(blob.name)
                    if len(names) == 256:
                        container.delete_blobs(*names)
                        names = []
                if names:
                    container.delete_blobs(*names)
    except Exception as exc:
        logger.warning("Blob cleanup failed for job %s: %s", job_id, exc)


def _login_allowed(request: Request, username: str) -> bool:
    """Redis-backed 10-attempt/minute login throttle; fail open on cache outage."""
    try:
        import hashlib
        import redis
        ip = request.client.host if request.client else "unknown"
        digest = hashlib.sha256(f"{ip}|{username.lower()}".encode()).hexdigest()
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3)
        key = f"ql:login:{digest}"
        attempts = client.eval("""
            local n=redis.call('INCR',KEYS[1])
            if n == 1 or redis.call('TTL',KEYS[1]) < 0 then redis.call('EXPIRE',KEYS[1],60) end
            return n
        """, 1, key)
        return attempts <= 10
    except Exception as exc:
        logger.debug("Login throttle unavailable: %s", exc)
        return True

IST = timezone(timedelta(hours=5, minutes=30))

def _to_ist(dt):
    """Convert a naive/UTC datetime to IST for API responses."""
    if dt is None:
        return None
    # DB datetimes are UTC but usually 'naive' (no tzinfo) — attach UTC first.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)
