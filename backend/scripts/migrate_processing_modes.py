"""Persist reuse/fresh policy. Run with API and workers stopped; no AI calls."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text
from app.database import engine


def migrate():
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("jobs")}
        if "processing_mode" not in columns:
            connection.execute(text("""
                ALTER TABLE jobs ADD processing_mode varchar(10) NOT NULL
                CONSTRAINT df_jobs_processing_mode DEFAULT 'reuse' WITH VALUES
            """))
        connection.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints
                WHERE parent_object_id=OBJECT_ID('jobs') AND name='ck_jobs_processing_mode')
                ALTER TABLE jobs WITH CHECK ADD CONSTRAINT ck_jobs_processing_mode
                CHECK (processing_mode IN ('reuse','fresh'))
        """))
    print("Processing modes ready. Existing jobs retain reuse behavior; no leads were reprocessed.")


if __name__ == "__main__":
    migrate()
