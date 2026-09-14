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
        logger.info("db_session: committing...")   # ← ADD
        db.commit()
        logger.info("db_session: committed OK")    # ← ADD
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
def locked_lead_session(lead_id: str):
    """A SQL session lock survives stage commits and serializes matching leads.

    SQL Server releases it on disconnect, including worker crashes. No Redis
    lease can expire mid-research and let a second worker repeat paid calls.
    """
    with engine.connect() as connection:
        db = Session(bind=connection, expire_on_commit=False)
        resource = None
        try:
            from app.models import Lead
            lead = db.get(Lead, lead_id)
            resource = "ql:process:" + str(lead.tracking_id or lead.id) if lead else None
            db.rollback()
            if resource and connection.dialect.name == "mssql":
                result = connection.execute(text("""
                    SET NOCOUNT ON;
                    DECLARE @result int;
                    EXEC @result = sp_getapplock @Resource=:resource,
                         @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=30000;
                    SELECT @result;
                """), {"resource": resource}).scalar()
                connection.commit()
                if result is None or result < 0:
                    resource = None
                    raise TimeoutError("Lead is being processed by another worker")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            if resource and connection.dialect.name == "mssql":
                try:
                    connection.execute(text(
                        "EXEC sp_releaseapplock @Resource=:resource, @LockOwner='Session'"
                    ), {"resource": resource})
                    connection.commit()
                except Exception:
                    connection.invalidate()  # Never return a locked connection to the pool.


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
                    j.reused_rows, j.research_reused_rows
                FROM leads l JOIN lead_tracking t ON t.id=l.tracking_id
                JOIN jobs j ON j.id=l.job_id
            """))
            if conn.execute(text("SELECT TOP 1 id FROM leads WHERE tracking_id IS NULL")).first():
                return False
        return True
    except Exception:
        logger.warning("Lead reuse schema is not ready; run scripts/migrate_lead_reuse.py")
        return False
