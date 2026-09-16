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
  GET    /jobs/{job_id}/export/excel  Download enriched .xlsx
  GET    /jobs/{job_id}/export/csv    Download enriched .csv
  DELETE /jobs/{job_id}               Delete job and all data
  GET    /health                      Health check

Start with:
    uvicorn app.main:app --reload --port 8000
"""

import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from azure.storage.blob import BlobServiceClient
from celery import chord
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, update
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings, SUPPORTED_EXTENSIONS
from app.database import get_db, ping_db, schema_ready
from app.models import Job, Lead, FinalScore, ValidationResult, AdminUser, LeadTracking
from app.utils.lead_identity import json_object, transaction_lock
from app.validators.evidence import proof_rows, clean_sources
from app.export.result_builder import build_excel_bytes, build_csv_bytes
from app.tasks.tasks import process_lead, finalise_job
from app.chat.routes import router as chat_router
from app.vectorstore.qdrant_store import delete_job as qdrant_delete_job
from app.auth import AuthenticationMiddleware, create_access_token, verify_password


logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AI Lead Qualification API",
    description="Ingest, validate, enrich, and score B2B sales leads.",
    version="1.0.0",
)
app.include_router(chat_router)

app.add_middleware(AuthenticationMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.APP_ENV.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class ICPRequest(BaseModel):
    icp_prompt: str = Field(min_length=1, max_length=8000)


class JobStatusResponse(BaseModel):
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
    created_at:     datetime
    completed_at:   Optional[datetime] = None
    llm_model:         Optional[str] = None
    embed_model:       Optional[str] = None
    total_tokens_used: Optional[int] = None
    prompt_tokens:     Optional[int] = None
    completion_tokens: Optional[int] = None


class LeadResultResponse(BaseModel):
    lead_id:        str
    reviewed: bool = False
    tracking_notes: str = ""
    tracking_version: int = 0
    reuse_kind: Optional[str] = None
    reused_from_lead_id: Optional[str] = None
    icp_input: Optional[str] = None
    icp_proofs: list[dict] = Field(default_factory=list)
    evidence_checked_at: Optional[str] = None
    employment_status: str = "unclear"
    contact_name:   Optional[str] = None
    company_name:   Optional[str] = None
    job_title:      Optional[str] = None
    email_address:  Optional[str] = None
    final_score:    Optional[float] = None
    priority:       Optional[str] = None
    reasoning:      Optional[str] = None
    growth_signals: Optional[str] = None
    outreach_intelligence: Optional[str] = None
    icp_score:      Optional[float] = None
    authority_score: Optional[float] = None
    data_score:     Optional[float] = None
    growth_score:   Optional[float] = None
    contact_score:  Optional[float] = None
    validation_status: Optional[str] = None
    source_urls:    list[str] = Field(default_factory=list)
    company_description: Optional[str] = None
    technologies_used:   Optional[str] = None


class PaginatedResults(BaseModel):
    """Wrapper for paginated /results — gives the frontend total + page info."""
    items:       list[LeadResultResponse]
    total:       int          # total leads for this job
    limit:       int          # page size used
    offset:      int          # starting index of this page
    returned:    int          # number of items in this page
    has_more:    bool         # whether more pages exist after this one


class TrackingUpdate(BaseModel):
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
async def upload_file(
    file: UploadFile = File(...),
    db:   Session    = Depends(get_db),
):
    """
    Accept an Excel/CSV upload and return IMMEDIATELY.

    Heavy work (parse + LLM column-mapping + dedup + bulk insert) runs in a
    background Celery task, so the request never times out on large files.
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
    raw_bytes = await file.read(max_bytes + 1)
    size_mb   = len(raw_bytes) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max: {settings.MAX_UPLOAD_SIZE_MB} MB",
        )

    # ── Create the job in 'importing' state ───────────────────────────────────
    job = Job(
        status=    "importing",
        total_rows= None,
        file_name=  file.filename,
        file_type=  suffix.lstrip("."),
    )
    db.add(job)
    db.flush()                      # get job.id
    job_id = str(job.id)

    # ── Store the raw file to Blob so the worker can read it ──────────────────
    blob_name = f"uploads/{job_id}/source{suffix}"
    if not await run_in_threadpool(_store_raw_to_blob, raw_bytes, blob_name):
        job.status = "import_failed"
        job.import_error = "Could not store the uploaded file. Check Blob storage."
        db.commit()
        raise HTTPException(status_code=503, detail="File storage unavailable — try again.")

    # ── Commit the job BEFORE dispatching (so the worker can find it) ─────────
    db.commit()

    # ── Dispatch the background import ────────────────────────────────────────
    from app.tasks.import_task import import_file
    import_file.delay(job_id, blob_name, suffix, file.filename)

    logger.info(f"Job {job_id} accepted for import: '{file.filename}'")

    return UploadResponse(
        job_id=    job_id,
        file_name= file.filename,
        file_type= suffix.lstrip("."),
        status=    "importing",
        message=   "File received. Import is running — poll /jobs/{job_id}/status.",
    )

# ── POST /jobs/{job_id}/icp ───────────────────────────────────────────────────
@app.post("/jobs/{job_id}/icp", status_code=status.HTTP_202_ACCEPTED)
def submit_icp(
    job_id:  str,
    body:    ICPRequest,
    db:      Session = Depends(get_db),
):
    """
    Attach an ICP prompt to a job and dispatch all Celery tasks.
    Returns immediately — frontend polls /jobs/{job_id}/status.
    """
    transaction_lock(db, "ql:dispatch:" + job_id)
    job: Job = _get_job_or_404(job_id, db)

    if job.status not in ("queued",):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already '{job.status}'. Only 'queued' jobs can be started.",
        )

    if not body.icp_prompt.strip():
        raise HTTPException(status_code=422, detail="icp_prompt cannot be empty.")

    # Save ICP prompt and mark running
    job.icp_prompt = body.icp_prompt.strip()
    job.status     = "running"
    db.flush()

    # Load all lead IDs for this job
    lead_ids = [str(r[0]) for r in db.query(Lead.id).filter(Lead.job_id == job_id).all()]

    if not lead_ids:
        raise HTTPException(status_code=422, detail="Job has no leads to process.")

    # Commit before dispatch so workers can always observe the running state.
    db.commit()
    try:
        header = [process_lead.s(lid, job.icp_prompt) for lid in lead_ids]
        callback = finalise_job.s(job_id)
        chord(header)(callback)
    except Exception as exc:
        job.status = "queued"
        db.commit()
        logger.exception("Failed to dispatch job %s", job_id)
        raise HTTPException(status_code=503, detail="Processing queue unavailable. Please retry.") from exc

    logger.info(f"Job {job_id}: dispatched {len(lead_ids)} tasks")

    return {
        "job_id":      job_id,
        "status":      "running",
        "tasks_queued": len(lead_ids),
        "message":     f"Processing {len(lead_ids)} leads. Poll /jobs/{job_id}/status for progress.",
    }


# ── GET /jobs ─────────────────────────────────────────────────────────────────
@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    """List all jobs, newest first."""
    jobs = db.query(Job).order_by(Job.created_at.desc()).limit(100).all()
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
    try:
        db.refresh(job)
    except Exception as e:
        logger.warning(f"db.refresh failed for job {job_id}: {e} — using cached state")
    return _job_to_status(job)


# ── GET /jobs/{job_id}/results ────────────────────────────────────────────────
@app.get("/jobs/{job_id}/results", response_model=PaginatedResults)
def job_results(
    job_id: str,
    limit:  int = Query(100, ge=1, le=500, description="Page size (max 500)."),
    offset: int = Query(0,  ge=0,          description="Rows to skip (page offset)."),
    search: Optional[str] = Query(None, max_length=100),
    priority: Optional[Literal["High", "Medium", "Low"]] = None,
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

    base = db.query(Lead).outerjoin(FinalScore, FinalScore.lead_id == Lead.id).filter(Lead.job_id == job_id)
    if search and search.strip():
        escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{escaped}%"
        base = base.filter(or_(
            Lead.company_name.ilike(term, escape="\\"), Lead.contact_name.ilike(term, escape="\\"),
            Lead.email_address.ilike(term, escape="\\"),
        ))
    if priority:
        base = base.filter(FinalScore.priority == priority)

    # Total count for the whole job (cheap COUNT query, no row hydration).
    total = base.with_entities(func.count(Lead.id)).scalar() or 0

    sort_columns = {
        "row_index": Lead.row_index,
        "company_name": Lead.company_name,
        "contact_name": Lead.contact_name,
        "email_address": Lead.email_address,
        "final_score": FinalScore.total_score,
        "priority": FinalScore.priority,
    }
    order_column = sort_columns[sort_by]
    order = order_column.desc() if sort_dir == "desc" else order_column.asc()
    leads = (
        base
        .options(
            joinedload(Lead.job), joinedload(Lead.final_score),           # 1-to-1 → JOIN in the same query
            selectinload(Lead.validation_results),  # 1-to-many → one extra IN() query
        )
        .order_by(order, Lead.row_index.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [_lead_to_response(lead) for lead in leads]

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
            selectinload(Lead.validation_results),
        )
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return _lead_to_response(lead)


# ── GET /jobs/{job_id}/export/excel ──────────────────────────────────────────
@app.patch("/jobs/{job_id}/results/{lead_id}/tracking", response_model=LeadResultResponse)
def update_tracking(job_id: str, lead_id: str, body: TrackingUpdate,
                    db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.job_id == job_id).first()
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
        raise HTTPException(status_code=422, detail="Provide reviewed or notes")
    changes.update(version=LeadTracking.version + 1, updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
    result = db.execute(update(LeadTracking).where(
        LeadTracking.id == lead.tracking_id, LeadTracking.version == body.version
    ).values(**changes).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Tracking changed elsewhere. Reload the lead and try again.")
    db.commit()
    db.expire_all()
    return _lead_to_response(lead)


@app.get("/jobs/{job_id}/export/excel")
def export_excel(job_id: str, db: Session = Depends(get_db)):
    """Build and STREAM the enriched .xlsx (no blob redirect → no CORS issues)."""
    job = _get_job_or_404(job_id, db)
    _require_completed(job)

    data = build_excel_bytes(job_id, db)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="job_{job_id}.xlsx"'},
    )


# ── GET /jobs/{job_id}/export/csv ─────────────────────────────────────────────
@app.get("/jobs/{job_id}/export/csv")
def export_csv(job_id: str, db: Session = Depends(get_db)):
    """Build and STREAM the enriched .csv (no blob redirect → no CORS issues)."""
    job = _get_job_or_404(job_id, db)
    _require_completed(job)

    data = build_csv_bytes(job_id, db)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="job_{job_id}.csv"'},
    )


from app.models import CompanyScrape

@app.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Session = Depends(get_db)):
    transaction_lock(db, "ql:dispatch:" + job_id)
    job = _get_job_or_404(job_id, db)

    try:
        qdrant_delete_job(job_id)
    except Exception as e:
        logger.warning(f"Failed to delete Qdrant vectors for job {job_id}: {e}")
    _delete_job_blobs(job_id)

    # Bulk delete — avoids ORM's uselist=False assumption missing duplicate rows
    db.query(CompanyScrape).filter(
        CompanyScrape.lead_id.in_(
            db.query(Lead.id).filter(Lead.job_id == job_id)
        )
    ).delete(synchronize_session=False)

    transaction_lock(db, "ql:lead-import")
    tracking_ids = [row[0] for row in db.query(Lead.tracking_id).filter(
        Lead.job_id == job_id, Lead.tracking_id.isnot(None)).distinct().all()]
    db.delete(job)
    db.flush()
    for start in range(0, len(tracking_ids), 500):
        db.query(LeadTracking).filter(
            LeadTracking.id.in_(tracking_ids[start:start + 500]),
            ~db.query(Lead.id).filter(Lead.tracking_id == LeadTracking.id).exists(),
        ).delete(synchronize_session=False)
    logger.info(f"Job {job_id} deleted")


@app.get("/jobs/{job_id}/token-report")
def token_report(job_id: str, db: Session = Depends(get_db)):
    """Per-stage token consumption breakdown for a job."""
    from sqlalchemy import func as sqlfunc
    from app.models import LLMCallLog
    _get_job_or_404(job_id, db)

    rows = (
        db.query(
            LLMCallLog.stage,
            LLMCallLog.module,
            sqlfunc.sum(LLMCallLog.prompt_tokens).label("prompt"),
            sqlfunc.sum(LLMCallLog.completion_tokens).label("completion"),
            sqlfunc.sum(LLMCallLog.total_tokens).label("total"),
            sqlfunc.count(LLMCallLog.id).label("calls"),
            sqlfunc.avg(LLMCallLog.duration_ms).label("avg_ms"),
        )
        .filter(LLMCallLog.job_id == job_id)
        .group_by(LLMCallLog.stage, LLMCallLog.module)
        .order_by(sqlfunc.sum(LLMCallLog.total_tokens).desc())
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
        progress_pct=       job.progress_pct,
        duplicates_removed= getattr(job, "duplicates_removed", None),
        import_error=       getattr(job, "import_error", None),
        created_at=         _to_ist(job.created_at),        # ← convert
        completed_at=       _to_ist(job.completed_at),      # ← convert
        llm_model=          getattr(job, "llm_model", None),
        embed_model=        getattr(job, "embed_model", None),
        total_tokens_used=  getattr(job, "total_tokens_used", None),
        prompt_tokens=      getattr(job, "prompt_tokens", None),
        completion_tokens=  getattr(job, "completion_tokens", None),
    )


def _lead_to_response(lead: Lead) -> LeadResultResponse:
    fs:   Optional[FinalScore]   = lead.final_score
    vrs:  list[ValidationResult] = lead.validation_results or []

    contact_vr = next((v for v in vrs if v.stage == "contact"), None)
    web_vr     = next((v for v in vrs if v.stage == "web"), None)

    source_urls: list[str] = []
    if web_vr and web_vr.sources_json:
        try:
            source_urls = json.loads(web_vr.sources_json)
        except Exception:
            pass

    icp_vr = next((v for v in vrs if v.stage == "icp"), None)
    web_data = json_object(web_vr.raw_output) if web_vr else {}
    icp_data = json_object(icp_vr.raw_output) if icp_vr else {}
    tracking = lead.tracking
    return LeadResultResponse(
        reviewed=bool(tracking.reviewed) if tracking else False,
        tracking_notes=(tracking.notes or "") if tracking else "",
        tracking_version=tracking.version if tracking else 0,
        reuse_kind=lead.reuse_kind,
        reused_from_lead_id=str(lead.reused_from_lead_id) if lead.reused_from_lead_id else None,
        icp_input=lead.job.icp_prompt,
        icp_proofs=proof_rows(web_data, icp_data),
        evidence_checked_at=web_data.get("checked_at") if isinstance(web_data.get("checked_at"), str) else None,
        employment_status=web_data.get("employment_status") if web_data.get("employment_status") in ("current", "former") else "unclear",
        lead_id=        str(lead.id),
        contact_name=   lead.contact_name,
        company_name=   lead.company_name,
        job_title=      lead.job_title,
        email_address=  lead.email_address,
        final_score=    fs.total_score    if fs else None,
        priority=       fs.priority       if fs else None,
        reasoning=      fs.reasoning      if fs else None,
        growth_signals= fs.growth_signals if fs else None,
        outreach_intelligence=fs.outreach_intelligence if fs else None,
        icp_score=      fs.icp_score      if fs else None,
        authority_score=fs.authority_score if fs else None,
        data_score=     fs.data_score     if fs else None,
        growth_score=   fs.growth_score   if fs else None,
        contact_score=  fs.contact_score  if fs else None,
        validation_status=("Passed" if contact_vr.passed else "Failed") if contact_vr else "Pending",
        source_urls=    clean_sources(source_urls),
        company_description=lead.company_description,
        technologies_used=  lead.technologies_used,
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

def _store_raw_to_blob(raw: bytes, blob_name: str) -> bool:
    """Upload raw bytes to Blob at the given name. Returns True on success."""
    try:
        client = BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING)
        container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
        try:
            container.get_container_properties()
        except Exception:
            try:
                container.create_container()
            except Exception:
                pass
        container.get_blob_client(blob_name).upload_blob(raw, overwrite=True)
        return True
    except Exception as e:
        logger.error(f"_store_raw_to_blob failed for '{blob_name}': {e}")
        return False


def _delete_job_blobs(job_id: str) -> None:
    """Best-effort cleanup of source, scrape, and export objects for a job."""
    try:
        client = BlobServiceClient.from_connection_string(settings.AZURE_BLOB_CONNECTION_STRING)
        container = client.get_container_client(settings.AZURE_BLOB_CONTAINER)
        for prefix in (f"uploads/{job_id}/", f"scraped/{job_id}/", f"exports/{job_id}/"):
            names = [blob.name for blob in container.list_blobs(name_starts_with=prefix)]
            for start in range(0, len(names), 256):
                container.delete_blobs(*names[start:start + 256])
    except Exception as exc:
        logger.warning("Blob cleanup failed for job %s: %s", job_id, exc)


def _login_allowed(request: Request, username: str) -> bool:
    """Redis-backed 10-attempt/minute login throttle; fail open on cache outage."""
    try:
        import hashlib
        import redis
        ip = request.client.host if request.client else "unknown"
        digest = hashlib.sha256(f"{ip}|{username.lower()}".encode()).hexdigest()
        client = redis.Redis.from_url(settings.REDIS_URL)
        key = f"ql:login:{digest}"
        attempts = client.incr(key)
        if attempts == 1:
            client.expire(key, 60)
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
