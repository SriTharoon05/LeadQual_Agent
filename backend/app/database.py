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
def ping_db() -> bool:
    """Return True if the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database ping failed: {e}")
        return False