import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEFAULTS = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_SQL_SERVER": "example.database.windows.net",
    "AZURE_SQL_DATABASE": "qualitylead",
    "AZURE_SQL_USERNAME": "testuser",
    "AZURE_SQL_PASSWORD": "testpass",
    "AZURE_BLOB_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=testacct;AccountKey=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;EndpointSuffix=core.windows.net",
    "QDRANT_URL": "https://example.qdrant.io",
    "QDRANT_API_KEY": "test-key",
    "AUTH_SECRET_KEY": "12345678901234567890123456789012",
}

for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)


# Isolated ORM integration database; no Azure credentials or network calls.
from datetime import datetime, timezone

import pytest
from app.database import Base
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mssql import UNIQUEIDENTIFIER
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(UNIQUEIDENTIFIER, "sqlite")
def sqlite_uuid(type_, compiler, **kwargs):
    return "CHAR(36)"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    engine.dialect.supports_native_uuid = True

    @event.listens_for(engine, "connect")
    def functions(connection, _):
        connection.create_function("getutcdate", 0, lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" "))
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()
