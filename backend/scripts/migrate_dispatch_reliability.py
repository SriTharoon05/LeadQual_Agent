"""Add durable upload/qualification publication cursors; no AI calls.

Back up SQL and stop every API/worker writer. Run this migration before other
pending ORM-based migrations because the current Job model uses these columns.
Existing terminal leads keep their saved results and are never reprocessed here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text
from app.database import engine
from app.models import CompanyScrape, Job


def migrate():
    if engine.dialect.name != "mssql":
        raise RuntimeError("This migration requires Azure SQL / SQL Server.")
    with engine.begin() as connection:
        if not inspect(connection).has_table("jobs"):
            raise RuntimeError("Initialize the base application schema first.")
        columns = {column["name"] for column in inspect(connection).get_columns("jobs")}
        added = False
        for name, ddl in {
            "source_blob_name": "nvarchar(1000) NULL",
            "upload_fingerprint": "varchar(64) NULL",
            "dispatch_row_index": "int NULL",
            "dispatch_lead_id": "uniqueidentifier NULL",
            "dispatch_complete": "bit NOT NULL CONSTRAINT df_jobs_dispatch_complete DEFAULT 0 WITH VALUES",
            "dispatch_updated_at": "datetime NULL",
            "dispatch_epoch": "varchar(64) NULL",
        }.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE jobs ADD {name} {ddl}"))
                added = True
        connection.execute(text("""
            UPDATE jobs SET source_blob_name = CONCAT('uploads/', LOWER(CONVERT(varchar(36), id)), '/source.', file_type)
            WHERE source_blob_name IS NULL AND upload_fingerprint IS NULL
                AND file_type IN ('csv','xlsx','xls','xlsm');
            UPDATE jobs SET dispatch_complete=1
            WHERE status IN ('completed','failed','import_failed');
        """))
        if added:
            # Active legacy jobs may have had only part of their chord published.
            # Replay pending rows safely; completed/failed rows are filtered out.
            connection.execute(text("""
                UPDATE j SET processed_rows=COALESCE(l.finished,0), failed_rows=COALESCE(l.failed,0),
                    total_rows=COALESCE(l.total,0)
                FROM jobs j LEFT JOIN (
                    SELECT job_id, COUNT(*) AS total,
                        SUM(CASE WHEN processing_status IN ('completed','failed') THEN 1 ELSE 0 END) AS finished,
                        SUM(CASE WHEN processing_status='failed' THEN 1 ELSE 0 END) AS failed
                    FROM leads GROUP BY job_id
                ) l ON l.job_id=j.id WHERE j.status='running';
            """))
        next(index for index in Job.__table__.indexes if index.name == "ix_jobs_dispatch_recovery").create(connection, checkfirst=True)
        CompanyScrape.__table__.create(connection, checkfirst=True)
        if inspect(connection).has_table("company_scrape"):
            scrape_columns = {column["name"] for column in inspect(connection).get_columns("company_scrape")}
            ingestion_added = False
            for name, ddl in {
                "ingest_status": "varchar(20) NOT NULL CONSTRAINT df_scrape_ingest_status DEFAULT 'queued' WITH VALUES",
                "ingest_attempts": "int NOT NULL CONSTRAINT df_scrape_ingest_attempts DEFAULT 0 WITH VALUES",
                "ingest_dispatched_at": "datetime NULL",
                "ingest_error": "nvarchar(1000) NULL",
            }.items():
                if name not in scrape_columns:
                    connection.execute(text(f"ALTER TABLE company_scrape ADD {name} {ddl}"))
                    ingestion_added = True
            if ingestion_added:
                connection.execute(text("""
                    UPDATE company_scrape SET ingest_status=CASE
                        WHEN embedded=1 THEN 'completed'
                        WHEN clean_text IS NOT NULL AND DATALENGTH(clean_text)>0 THEN 'embedding'
                        ELSE 'queued' END;
                """))
            column = connection.execute(text("""
                SELECT TYPE_NAME(user_type_id), max_length FROM sys.columns
                WHERE object_id=OBJECT_ID('company_scrape') AND name='clean_text'
            """)).first()
            if column and (column[0] != "nvarchar" or column[1] != -1):
                connection.execute(text("ALTER TABLE company_scrape ALTER COLUMN clean_text nvarchar(max) NULL"))
            for index in CompanyScrape.__table__.indexes:
                if index.name in {"ix_scrape_latest", "ix_scrape_ingestion"}:
                    index.create(connection, checkfirst=True)
    print("Dispatch reliability migration complete. Deploy matching API and all workers.")


if __name__ == "__main__":
    migrate()
