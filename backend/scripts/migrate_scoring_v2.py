"""Add and backfill deterministic qualification using saved evidence only.

Back up Azure SQL, let active jobs/refreshes finish, stop API/workers, then run
from backend: python scripts/migrate_scoring_v2.py
No web research, email sending, DNS checks or LLM calls are performed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, or_, text
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import engine
from app.models import FinalScore, Job, Lead, LeadRefresh
from app.scoring.policy import SCORING_VERSION, icp_assessment
from app.scoring.scoring_engine import score_saved
from app.utils.lead_identity import json_object


def migrate():
    if engine.dialect.name != "mssql":
        raise RuntimeError("This migration requires Azure SQL / SQL Server.")
    with engine.begin() as connection:
        if connection.execute(text("SELECT TOP 1 id FROM jobs WHERE status IN ('running','importing')")).first():
            raise RuntimeError("Let active jobs finish and stop API/workers before migrating.")
        if inspect(connection).has_table("lead_refresh") and connection.execute(text(
                "SELECT TOP 1 id FROM lead_refresh WHERE status IN ('queued','running')")).first():
            raise RuntimeError("Let evidence refreshes finish before migrating.")
        columns = {column["name"] for column in inspect(connection).get_columns("final_scores")}
        for name, ddl in {"scoring_version": "varchar(32) NULL", "qualification_status": "varchar(24) NULL",
                          "scoring_details_json": "nvarchar(max) NULL", "scoring_audit_json": "nvarchar(max) NULL"}.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE final_scores ADD {name} {ddl}"))
        # Schema/backfill commit together. A failed upgrade cannot expose mixed
        # score policies to sorting, filters, exports or the chatbot.
        with Session(bind=connection, expire_on_commit=False) as db:
            count = 0
            last_id = None
            while True:
                query = db.query(FinalScore).filter(or_(FinalScore.scoring_version.is_(None), FinalScore.scoring_version != SCORING_VERSION))
                if last_id is not None:
                    query = query.filter(FinalScore.id > last_id)
                batch = query.options(joinedload(FinalScore.lead).joinedload(Lead.job),
                                      joinedload(FinalScore.lead).selectinload(Lead.validation_results)).order_by(FinalScore.id).limit(200).all()
                if not batch:
                    break
                for final in batch:
                    lead = final.lead
                    if lead is None or lead.job is None:
                        raise RuntimeError("An orphaned score exists. Repair its lead/job before migrating.")
                    stages = {row.stage: row for row in lead.validation_results}
                    payload = {"Contact Name": lead.contact_name, "Company Name": lead.company_name,
                               "Job Title": lead.job_title, "Email Address": lead.email_address,
                               "Phone Number": lead.phone_number, "Direct Phone Number": lead.direct_phone_number}
                    web = json_object(stages["web"].raw_output) if "web" in stages else {}
                    icp = json_object(stages["icp"].raw_output) if "icp" in stages else {}
                    contact = json_object(stages["contact"].raw_output) if "contact" in stages else {}
                    result, checks, contact_data = score_saved(payload, icp, web, contact, lead.job.icp_prompt)
                    audit = {"previous_score": {column.name: getattr(final, column.name) for column in FinalScore.__table__.columns if column.name != "scoring_audit_json"},
                             "previous_icp": icp, "previous_contact": contact, "migration": SCORING_VERSION}
                    details = json.loads(result.scoring_details_json)
                    for name in ("total_score", "icp_score", "contact_score", "growth_score", "priority", "qualification_status",
                                 "scoring_version", "reasoning", "growth_signals", "outreach_intelligence"):
                        setattr(final, name, getattr(result, name))
                    final.scoring_details_json = json.dumps(details, ensure_ascii=False, default=str)
                    final.scoring_audit_json = json.dumps(audit, ensure_ascii=False, default=str)
                    final.authority_score = final.data_score = None
                    if "icp" in stages:
                        fit = icp_assessment(checks)
                        stages["icp"].raw_output = json.dumps({**icp, "checks": checks, "icp_match": fit["match"],
                            "icp_score": fit["score"] / 10, "major_disqualifiers": fit["mismatches"], "scoring_version": SCORING_VERSION})
                        stages["icp"].score, stages["icp"].passed = fit["score"] / 10, fit["match"]
                    if "contact" in stages:
                        stages["contact"].raw_output = json.dumps(contact_data)
                    count += 1
                last_id = batch[-1].id
                db.flush()
                db.expunge_all()
            print(f"Recalculated {count} saved lead scores using {SCORING_VERSION}.")
    print("Migration complete. Deploy the matching frontend, API and workers together.")


if __name__ == "__main__":
    migrate()
