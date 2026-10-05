"""Run once with API/workers stopped: python scripts/migrate_lead_reuse.py.

Idempotent additive Azure SQL migration. Back up the DB first. Existing duplicate
stage/score rows cause a preflight failure; this script never deletes user data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import engine
from app.models import Lead, LeadTracking, TeamMember
from app.utils.lead_identity import assign_identity
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

LEAD_COLUMNS = {
    "tracking_id": "uniqueidentifier NULL",
    "crm_key": "varchar(64) NULL",
    "email_key": "varchar(64) NULL",
    "linkedin_key": "varchar(64) NULL",
    "person_company_key": "varchar(64) NULL",
    "input_fingerprint": "varchar(64) NULL",
    "research_fingerprint": "varchar(64) NULL",
    "reused_from_lead_id": "uniqueidentifier NULL",
    "reuse_kind": "varchar(20) NULL",
}


def migrate():
    if engine.dialect.name != "mssql":
        raise RuntimeError("This migration requires Azure SQL / SQL Server")
    with engine.begin() as connection:
        for table, grouping in (("final_scores", "lead_id"), ("validation_results", "lead_id, stage")):
            duplicate = connection.execute(text(
                f"SELECT TOP 1 {grouping} FROM {table} GROUP BY {grouping} HAVING COUNT(*) > 1"
            )).first()
            if duplicate:
                raise RuntimeError(f"Duplicate rows in {table}; review them before migrating. No data changed.")
        if connection.execute(text(
            "SELECT TOP 1 id FROM jobs WHERE status IN ('running', 'importing')"
        )).first():
            raise RuntimeError("Let active jobs finish and stop API/workers before migrating.")
        TeamMember.__table__.create(connection, checkfirst=True)
        LeadTracking.__table__.create(connection, checkfirst=True)
        columns = {column["name"] for column in inspect(connection).get_columns("leads")}
        for name, ddl in LEAD_COLUMNS.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE leads ADD {name} {ddl}"))
        job_columns = {column["name"] for column in inspect(connection).get_columns("jobs")}
        for name in ("reused_rows", "research_reused_rows"):
            if name not in job_columns:
                connection.execute(text(
                    f"ALTER TABLE jobs ADD {name} int NOT NULL CONSTRAINT df_jobs_{name} DEFAULT 0 WITH VALUES"))
        fks = inspect(connection).get_foreign_keys("leads")
        if not any(fk["constrained_columns"] == ["tracking_id"] for fk in fks):
            connection.execute(text("ALTER TABLE leads ADD CONSTRAINT fk_leads_tracking "
                                    "FOREIGN KEY (tracking_id) REFERENCES lead_tracking(id)"))
        for key in ("tracking_id", "crm_key", "email_key", "linkedin_key", "person_company_key"):
            index = next(i for i in Lead.__table__.indexes if i.name == "ix_leads_" + key)
            index.create(connection, checkfirst=True)
        for name, table, columns in (
            ("uq_final_score_lead", "final_scores", "lead_id"),
            ("uq_validation_lead_stage", "validation_results", "lead_id, stage"),
        ):
            exists = connection.execute(text("SELECT 1 FROM sys.indexes WHERE name=:name"), {"name": name}).first()
            if not exists:
                connection.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({columns})"))
    # Current ORM relationships need their additive workspace schema before
    # hydrating legacy leads. This also preserves old standalone upgrade paths.
    from scripts.migrate_team_workflow import migrate as migrate_workflow
    migrate_workflow()
    # Restartable batches. Old results retain original dates and imported notes.
    count = 0
    with Session(engine, expire_on_commit=False) as db:
        while True:
            leads = db.query(Lead).filter(Lead.tracking_id.is_(None)).order_by(Lead.created_at, Lead.id).limit(200).all()
            if not leads:
                break
            for lead in leads:
                record = {column.name: getattr(lead, column.name) for column in Lead.__table__.columns}
                assign_identity(db, record)
                for key in LEAD_COLUMNS:
                    setattr(lead, key, record.get(key))
                db.flush()
                count += 1
            db.commit()
            print(f"Backfilled {count} leads")
    print("Migration complete. Start the updated API and workers together.")


if __name__ == "__main__":
    migrate()
