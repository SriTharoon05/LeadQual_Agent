"""Add team workflow and evidence refresh storage without deleting lead data.

Run from backend: python scripts/migrate_team_workflow.py
Back up Azure SQL first; stop the API and workers and let active jobs finish.
This script is additive and restartable. It does not run application research.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.database import engine
from app.models import Lead, LeadRefresh, LeadTracking, SavedSegment, TeamMember


TRACKING_COLUMNS = {
    "owner_id": "uniqueidentifier NULL",
    "follow_up_at": "datetime NULL",
    "follow_up_note": "nvarchar(max) NULL",
    "follow_up_completed_at": "datetime NULL",
}
REFRESH_COLUMNS = {
    "attempt_count": "int NOT NULL CONSTRAINT df_refresh_attempt_count DEFAULT 0 WITH VALUES",
    "last_dispatched_at": "datetime NULL",
    "previous_snapshot_json": "nvarchar(max) NULL",
}


def _add_columns(connection, table: str, additions: dict[str, str]) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns(table)}
    for name, ddl in additions.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE {table} ADD {name} {ddl}"))


def migrate() -> None:
    if engine.dialect.name != "mssql":
        raise RuntimeError("This migration requires Azure SQL / SQL Server.")
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("leads") or not inspector.has_table("jobs"):
            raise RuntimeError("Initialize the application schema before migrating.")
        if connection.execute(text(
                "SELECT TOP 1 id FROM jobs WHERE status IN ('running', 'importing')")).first():
            raise RuntimeError("Let active jobs finish, then stop API/workers before migrating.")
        if inspector.has_table("lead_refresh") and connection.execute(text(
                "SELECT TOP 1 id FROM lead_refresh WHERE status IN ('queued', 'running')")).first():
            raise RuntimeError("Let active evidence refreshes finish before migrating.")

        # The owner target must exist before creating lead_tracking's FK.
        TeamMember.__table__.create(connection, checkfirst=True)
        SavedSegment.__table__.create(connection, checkfirst=True)
        LeadTracking.__table__.create(connection, checkfirst=True)
        _add_columns(connection, "lead_tracking", TRACKING_COLUMNS)
        _add_columns(connection, "leads", {"evidence_checked_at": "datetime NULL"})
        LeadRefresh.__table__.create(connection, checkfirst=True)
        _add_columns(connection, "lead_refresh", REFRESH_COLUMNS)

        foreign_keys = inspect(connection).get_foreign_keys("lead_tracking")
        if not any(fk["constrained_columns"] == ["owner_id"] for fk in foreign_keys):
            connection.execute(text(
                "ALTER TABLE lead_tracking ADD CONSTRAINT fk_tracking_owner "
                "FOREIGN KEY (owner_id) REFERENCES team_member(id)"))
        for table, names in (
            (LeadTracking.__table__, {"ix_lead_tracking_owner_id", "ix_tracking_owner_due"}),
            (Lead.__table__, {"ix_leads_evidence_checked_at"}),
            (LeadRefresh.__table__, {"ix_lead_refresh_lead_id", "uq_lead_refresh_active", "ix_refresh_dispatch"}),
        ):
            for index in table.indexes:
                if index.name in names:
                    index.create(connection, checkfirst=True)

        # Only an actual successful web check supplies a freshness date.
        # Invalid JSON, errors, missing dates and future dates remain unknown.
        connection.execute(text("""
            WITH web_payload AS (
                SELECT lead_id, CASE WHEN ISJSON(CONVERT(nvarchar(max), raw_output)) = 1
                    THEN CONVERT(nvarchar(max), raw_output) ELSE N'{}' END AS payload
                FROM validation_results WHERE stage = 'web'
            ), checked AS (
                SELECT lead_id,
                    TRY_CONVERT(datetimeoffset, JSON_VALUE(payload, '$.checked_at')) AS checked_at,
                    JSON_VALUE(payload, '$.error') AS error
                FROM web_payload
            )
            UPDATE l SET evidence_checked_at = CONVERT(datetime,
                SWITCHOFFSET(c.checked_at, '+00:00'))
            FROM leads l JOIN checked c ON c.lead_id = l.id
            WHERE l.evidence_checked_at IS NULL AND c.checked_at IS NOT NULL
                AND c.checked_at >= '1753-01-01T00:00:00+00:00'
                AND c.checked_at <= SYSDATETIMEOFFSET()
                AND COALESCE(c.error, '') = ''
        """))
    print("Team workflow migration complete. Start the updated API and workers together.")


if __name__ == "__main__":
    migrate()
