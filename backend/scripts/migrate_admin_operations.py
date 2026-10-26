"""Add runtime settings, cost snapshots and operational events; never call AI.

Stop API/worker writers and apply prior documented migrations first. This is
idempotent. Existing assignment/reminder tables are preserved as inactive data.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select, text
from app.database import engine
from app.models import LLMCallLog, OperationalEvent, RuntimeSettings
from app.admin.settings import DEFAULT_PRICING
from app.admin.telemetry import safe_message


def migrate(copy_warnings=True):
    with engine.begin() as connection:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens_used"):
            connection.execute(text(f"""
                IF EXISTS (SELECT 1 FROM sys.columns c JOIN sys.types t ON t.user_type_id=c.user_type_id
                    WHERE c.object_id=OBJECT_ID('jobs') AND c.name='{name}' AND t.name='int')
                    ALTER TABLE jobs ALTER COLUMN [{name}] bigint NULL
            """))
        if not inspect(connection).has_table("llm_call_logs"):
            LLMCallLog.__table__.create(connection)
        existing = {column["name"] for column in inspect(connection).get_columns("llm_call_logs")}
        additions = {
            "billing_model": "varchar(100) NULL", "cached_prompt_tokens": "int NULL",
            "search_requests": "int NULL", "status": "varchar(24) NULL", "error_code": "varchar(80) NULL",
            "provider_request_id": "varchar(200) NULL", "cost_complete": "bit NULL",
            "pricing_snapshot_json": "nvarchar(max) NULL",
            **{name: "numeric(20,10) NULL" for name in ("cost_input_usd", "cost_cached_usd", "cost_output_usd", "cost_search_usd", "cost_total_usd")},
        }
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE llm_call_logs ADD [{name}] {definition}"))
        for index in LLMCallLog.__table__.indexes:
            index.create(connection, checkfirst=True)
        RuntimeSettings.__table__.create(connection, checkfirst=True)
        OperationalEvent.__table__.create(connection, checkfirst=True)
        exists = connection.execute(select(RuntimeSettings.id).where(RuntimeSettings.id == "runtime")).first()
        if not exists:
            connection.execute(RuntimeSettings.__table__.insert().values(id="runtime", concurrency=5,
                pricing_json=json.dumps(DEFAULT_PRICING), version=0))
    copied = 0
    if copy_warnings and inspect(engine).has_table("processing_logs"):
        cursor = None
        while True:
            with engine.begin() as connection:
                predicate = " AND id > CAST(:cursor AS uniqueidentifier)" if cursor else ""
                rows = connection.execute(text("SELECT TOP(1000) id, job_id, lead_id, level, message, created_at "
                    "FROM processing_logs WHERE level IN ('WARNING','ERROR')" + predicate + " ORDER BY id"),
                    {"cursor": cursor} if cursor else {}).mappings().all()
                if not rows:
                    break
                ids = [str(row["id"]) for row in rows]
                existing_ids = set(connection.execute(select(OperationalEvent.event_id).where(
                    OperationalEvent.event_id.in_(ids))).scalars())
                values = []
                for row in rows:
                    event_id = str(row["id"])
                    if event_id in existing_ids:
                        continue
                    message = safe_message(row["message"])
                    is_rate = "429" in message or "rate limit" in message.lower()
                    values.append({"event_id": event_id, "job_id": row["job_id"], "lead_id": row["lead_id"],
                        "severity": str(row["level"]).upper(), "kind": "rate_limit" if is_rate else "error" if str(row["level"]).upper() == "ERROR" else "warning",
                        "component": "legacy.processing_logs", "message": message,
                        "occurred_at": row["created_at"]})
                if values:
                    connection.execute(OperationalEvent.__table__.insert(), values)
                    copied += len(values)
                cursor = str(rows[-1]["id"])
    print(f"Admin operations schema ready; copied {copied} existing warning/error records. No AI, DNS, or pricing-provider calls were made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-existing-warnings", action="store_true", help="Add schema only; copy historical warnings on a later rerun")
    args = parser.parse_args()
    migrate(copy_warnings=not args.skip_existing_warnings)
