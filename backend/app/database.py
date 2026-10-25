"""
database.py
───────────
SQLAlchemy engine + session factory for Azure SQL.

Import pattern everywhere else:
    from app.database import get_db, engine

Never create an engine or session outside this file.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.pool import QueuePool

from app.config import settings

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
engine = create_engine(
    settings.azure_sql_connection_string,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,          # test connections before using them
    pool_recycle=300,           # recycle connections every 30 min
    echo=(settings.LOG_LEVEL == "DEBUG"),
    use_insertmanyvalues=False,
    connect_args={
        "timeout": 30,
        "ConnectRetryCount": 3,
        "ConnectRetryInterval": 5,        # ← connection timeout in seconds
    },
)


# Keep Azure SQL connections alive (prevent idle timeout drops)
@event.listens_for(engine, "connect")
def set_connection_options(dbapi_connection, connection_record):
    pass  # pyodbc handles keep-alive via the connection string


# ── Session factory ───────────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,   # safe for background Celery tasks
)


# ── Base class for ORM models ─────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Dependency for FastAPI routes ─────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency. Yields a DB session and guarantees cleanup.

    Usage in a route:
        @router.get("/something")
        def my_route(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Context manager for non-FastAPI code (Celery tasks, scripts) ──────────────
from celery.exceptions import Retry

@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Retry:
        db.rollback()
        raise
    except Exception as e:
        logger.error(f"db_session commit failed, rolling back: {e}", exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


# ── Health check ──────────────────────────────────────────────────────────────
@contextmanager
def locked_chat_session(conversation_id: str):
    """Serialize a conversation while allowing commits to return SQL connections.

    Long model/vector waits hold only a renewable Redis lease. Every SQL commit
    checks ownership; chat routes also take a short transaction lock when saving
    ordered turns. A failed lease cannot authorize a stale writer.
    """
    from app.utils.chat_lease import ConversationLease
    lease = ConversationLease(conversation_id)
    db = None
    try:
        lease.acquire()
        db = SessionLocal()
        event.listen(db, "before_commit", lease.assert_owned)
        yield db
        db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            lease.release()


@contextmanager
def locked_lead_session(lead_id: str, *, lock_timeout_ms: int = 30000, artifact_shared: bool = False):
    """A SQL session lock survives stage commits and serializes matching leads.

    SQL Server releases it on disconnect, including worker crashes. No Redis
    lease can expire mid-research and let a second worker repeat paid calls.
    """
    with engine.connect() as connection:
        db = Session(bind=connection, expire_on_commit=False)
        resource = None
        acquired = False
        attempted = False
        acquisition_known = False
        artifact_resource = None
        artifact_acquired = artifact_attempted = artifact_known = False
        try:
            from app.models import Lead
            lead = db.query(Lead.id, Lead.tracking_id, Lead.job_id).filter(Lead.id == lead_id).first()
            resource = "ql:process:" + str(lead.tracking_id or lead.id) if lead else None
            artifact_resource = "ql:artifacts:" + str(lead.job_id) if lead and artifact_shared else None
            db.rollback()
            if resource and connection.dialect.name == "mssql":
                attempted = True
                result = connection.execute(text("""
                    SET NOCOUNT ON;
                    DECLARE @result int;
                    EXEC @result = sp_getapplock @Resource=:resource,
                         @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=:timeout_ms;
                    SELECT @result;
                """), {"resource": resource, "timeout_ms": max(0, min(int(lock_timeout_ms), 30000))}).scalar()
                acquisition_known = result is not None
                acquired = result is not None and result >= 0
                connection.commit()
                if not acquired:
                    raise TimeoutError("Lead is being processed by another worker")
            if artifact_resource and connection.dialect.name == "mssql":
                artifact_attempted = True
                code = connection.execute(text("""
                    SET NOCOUNT ON;
                    DECLARE @result int;
                    EXEC @result = sp_getapplock @Resource=:resource,
                        @LockMode='Shared', @LockOwner='Session', @LockTimeout=:timeout_ms;
                    SELECT @result;
                """), {"resource": artifact_resource,
                         "timeout_ms": max(0, min(int(lock_timeout_ms), 1000))}).scalar()
                artifact_known = code is not None
                artifact_acquired = code is not None and code >= 0
                connection.commit()
                if not artifact_acquired:
                    raise TimeoutError("This upload is being deleted")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            try:
                db.close()
            finally:
                resources = ([artifact_resource] if artifact_acquired else []) + ([resource] if acquired else [])
                _release_session_resources(connection, resources,
                    uncertain=(attempted and not acquisition_known) or (artifact_attempted and not artifact_known))


@contextmanager
def locked_import_session(job_id: str):
    """Serialize paid parsing/mapping across import retries and recovery tasks."""
    with engine.connect() as connection:
        db = Session(bind=connection, expire_on_commit=False)
        acquired = False
        attempted = connection.dialect.name == "mssql"
        acquisition_known = False
        resource = "ql:import:" + job_id
        try:
            if attempted:
                code = connection.execute(text("""
                    SET NOCOUNT ON;
                    DECLARE @result int;
                    EXEC @result = sp_getapplock @Resource=:resource,
                        @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=1000;
                    SELECT @result;
                """), {"resource": resource}).scalar()
                acquisition_known = code is not None
                acquired = code is not None and code >= 0
                connection.commit()
                if not acquired:
                    raise TimeoutError("This upload is already importing")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            try:
                db.close()
            finally:
                _release_session_resources(connection, [resource] if acquired else [],
                    uncertain=attempted and not acquisition_known)


def _release_session_resources(connection, resources, *, uncertain=False):
    """Never pool a socket with unconfirmed SQL session locks, even on close errors."""
    if connection.dialect.name != "mssql":
        return
    try:
        if uncertain:
            connection.invalidate()
            return
        for resource in resources:
            code = connection.execute(text("""
                SET NOCOUNT ON;
                DECLARE @result int;
                EXEC @result = sp_releaseapplock @Resource=:resource, @LockOwner='Session';
                SELECT @result;
            """), {"resource": resource}).scalar()
            connection.commit()
            if code is None or code < 0:
                connection.invalidate()
                return
    except Exception:
        try:
            connection.invalidate()
        except Exception:
            logger.warning("Could not explicitly invalidate a failed SQL session-lock connection")


def ping_db() -> bool:
    """Return True if the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database ping failed: {e}")
        return False


def schema_ready() -> bool:
    """Prevent routing traffic to new code before its migration/backfill is done."""
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                SELECT TOP 0 l.tracking_id, l.crm_key, l.email_key, l.linkedin_key,
                    l.person_company_key, l.input_fingerprint, l.research_fingerprint,
                    l.reused_from_lead_id, l.reuse_kind, t.reviewed, t.notes, t.version,
                    j.reused_rows, j.research_reused_rows, l.evidence_checked_at,
                    j.source_blob_name, j.upload_fingerprint, j.dispatch_row_index, j.dispatch_lead_id,
                    j.dispatch_complete, j.dispatch_updated_at, j.dispatch_epoch,
                    t.owner_id, t.follow_up_at, t.follow_up_note, t.follow_up_completed_at
                FROM leads l JOIN lead_tracking t ON t.id=l.tracking_id
                JOIN jobs j ON j.id=l.job_id
            """))
            if conn.execute(text("SELECT TOP 1 id FROM leads WHERE tracking_id IS NULL")).first():
                return False
            conn.execute(text("""
                SELECT TOP 0 id, name, email, active, version, created_at, updated_at FROM team_member
            """))
            conn.execute(text("""
                SELECT TOP 0 id, name, filters_json, version, created_at, updated_at FROM saved_segment
            """))
            conn.execute(text("""
                SELECT TOP 0 id, lead_id, status, requested_by, created_at, started_at,
                    finished_at, error_message, attempt_count, last_dispatched_at,
                    previous_snapshot_json FROM lead_refresh
            """))
            conn.execute(text("SELECT TOP 0 scoring_version, qualification_status, scoring_details_json, scoring_audit_json FROM final_scores"))
            conn.execute(text("""
                SELECT TOP 0 ingest_status, ingest_attempts, ingest_dispatched_at, ingest_error
                FROM company_scrape
            """))
            if conn.execute(text("""
                SELECT TOP 1 id FROM final_scores
                WHERE scoring_version IS NULL OR scoring_version <> 'icp_contact_growth_v2'
            """)).first():
                return False
            if conn.execute(text("""
                SELECT COUNT(*) FROM sys.indexes
                WHERE object_id=OBJECT_ID('lead_refresh') AND is_unique=1 AND has_filter=1
                    AND name='uq_lead_refresh_active'
            """)).scalar() != 1:
                return False
            conn.execute(text("""
                SELECT TOP 0 m.conversation_id, m.request_id, m.[sequence],
                    m.tool_used, m.sources_json, m.metadata_json, c.next_sequence
                FROM chat_message m JOIN chat_conversation c ON c.id=m.conversation_id
            """))
            if conn.execute(text("""
                SELECT TOP 1 m.id FROM chat_message m
                LEFT JOIN chat_conversation c ON c.id=m.conversation_id
                WHERE m.conversation_id IS NULL OR m.request_id IS NULL
                    OR m.[sequence] IS NULL OR c.id IS NULL OR c.job_id <> m.job_id
            """)).first():
                return False
            if conn.execute(text("""
                SELECT COUNT(*) FROM sys.indexes
                WHERE object_id=OBJECT_ID('chat_message') AND is_unique=1
                    AND name IN ('uq_chat_conversation_sequence', 'uq_chat_request_role')
            """)).scalar() != 2:
                return False
        return True
    except Exception:
        logger.warning("Schema is not ready; apply the lead reuse, chat reliability, team workflow and scoring v2 migrations")
        return False
