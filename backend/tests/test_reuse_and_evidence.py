import io
import json
import uuid
from types import SimpleNamespace

import pandas as pd
import pytest
from app.auth import create_access_token
from app.database import get_db
from app.main import _lead_to_response, app
from app.models import FinalScore, Job, Lead, ValidationResult
from app.tasks import tasks
from app.utils.deduplicator import deduplicate
from app.utils.lead_identity import (
    assign_identity,
    copy_result,
    identity_fields,
    reusable_lead,
)
from app.validators.evidence import normalize_evidence, proof_rows, safe_url
from app.validators.icp_validator import _validated_checks
from app.validators.web_researcher import _merge
from fastapi.testclient import TestClient
from sqlalchemy import event


def add_lead(db, *, icp="US technology companies", completed=False, **fields):
    job = Job(id=str(uuid.uuid4()), icp_prompt=icp, status="running", total_rows=1,
              processed_rows=int(completed), failed_rows=0, reused_rows=0, research_reused_rows=0)
    db.add(job)
    db.flush()
    record = {"id": str(uuid.uuid4()), "job_id": job.id, "contact_name": "Alice Smith",
                  "company_name": "Acme", "company_website": "https://acme.example",
                  "email_address": "alice@acme.example", "job_title": "Director",
                  "processing_status": "completed" if completed else "pending"}
    record.update(fields)
    assign_identity(db, record)
    lead = Lead(**record)
    db.add(lead)
    db.flush()
    if completed:
        for stage in ("icp", "web", "contact"):
            data = {"evidence": normalize_evidence({}, []), "checked_at": "2026-01-01T00:00:00+00:00"} if stage == "web" else {}
            db.add(ValidationResult(lead_id=lead.id, stage=stage, passed=True, score=7,
                                    raw_output=json.dumps(data)))
        db.add(FinalScore(lead_id=lead.id, total_score=70, priority="Medium", reasoning="Saved summary"))
    db.commit()
    db.expire_all()
    return db.get(Lead, lead.id)


def test_same_lead_different_upload_reuses_database_result(db, monkeypatch):
    first = add_lead(db, completed=True)
    second = add_lead(db)
    assert first.tracking_id == second.tracking_id
    assert reusable_lead(db, second, second.job.icp_prompt)[1] == "full"

    def unexpected(*args, **kwargs):
        pytest.fail("Duplicate lead triggered processing")
    for name in ("research_lead", "validate_icp", "verify_contact", "score_lead", "persist_and_dispatch_embed"):
        monkeypatch.setattr(tasks, name, unexpected)
    tasks._process_lead(SimpleNamespace(), db, second.id, second.job.icp_prompt)
    db.commit()
    db.expire_all()
    assert second.processing_status == "completed"
    assert second.final_score.total_score == first.final_score.total_score
    assert second.job.processed_rows == 1
    assert second.job.reused_rows == 1
    assert len(second.validation_results) == 3
    # A redelivered task does not change counters or insert another snapshot.
    tasks._process_lead(SimpleNamespace(), db, second.id, second.job.icp_prompt)
    db.commit()
    assert second.job.processed_rows == 1
    assert db.query(FinalScore).filter_by(lead_id=second.id).count() == 1


def test_changed_icp_reuses_research_not_verdict(db):
    add_lead(db, completed=True)
    lead = add_lead(db, icp="European manufacturing companies")
    assert reusable_lead(db, lead, lead.job.icp_prompt)[1] == "research"


def test_changed_role_or_company_does_not_reuse_employment_proof(db):
    add_lead(db, completed=True)
    moved = add_lead(db, company_name="Other Inc")
    assert reusable_lead(db, moved, moved.job.icp_prompt) == (None, None)
    promoted = add_lead(db, job_title="CEO")
    assert reusable_lead(db, promoted, promoted.job.icp_prompt) == (None, None)


def test_conflicting_people_and_failed_results_are_not_reused(db):
    first = add_lead(db, completed=True)
    different = add_lead(db, email_address="someoneelse@acme.example")
    assert first.tracking_id != different.tracking_id
    first.processing_status = "failed"
    db.commit()
    repeat = add_lead(db)
    assert reusable_lead(db, repeat, repeat.job.icp_prompt) == (None, None)


def test_result_and_tracking_survive_source_job_deletion(db):
    source = add_lead(db, completed=True)
    source.tracking.notes, source.tracking.reviewed = "Call next week", True
    target = add_lead(db)
    copy_result(db, source, target)
    db.commit()
    db.delete(source.job)
    db.commit()
    db.expire_all()
    assert target.final_score.total_score == 70
    assert len(target.validation_results) == 3
    assert target.tracking.notes == "Call next week"
    assert target.tracking.reviewed


def test_tracking_api_persists_shared_changes_and_rejects_stale_version(db):
    first, second = add_lead(db, completed=True), add_lead(db)
    def override():
        yield db
    app.dependency_overrides[get_db] = override
    try:
        client = TestClient(app, base_url="http://localhost")
        path = f"/jobs/{first.job_id}/results/{first.id}/tracking"
        headers = {"Authorization": "Bearer " + create_access_token("admin")}
        assert client.patch(path, json={"version": 0, "reviewed": True}).status_code == 401
        result = client.patch(path, headers=headers, json={"version": 0, "reviewed": True, "notes": "Call Friday"})
        assert result.status_code == 200, result.text
        assert result.json()["tracking_version"] == 1
        assert _lead_to_response(second).tracking_notes == "Call Friday"
        stale = client.patch(path, headers=headers, json={"version": 0, "notes": "overwrite"})
        assert stale.status_code == 409
        assert client.patch(path, headers=headers, json={"version": 1, "notes": None}).status_code == 422
        assert client.patch(path, headers=headers, json={"version": 1, "notes": "x" * 10001}).status_code == 422
        wrong_job = f"/jobs/{second.job_id}/results/{first.id}/tracking"
        assert client.patch(wrong_job, headers=headers, json={"version": 1, "reviewed": False}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_sources_must_be_real_search_results_with_supporting_excerpts():
    url = "https://company.example/team"
    raw = {"employment": {"status": "confirmed", "value": "current", "sources": [
        {"url": url, "title": "Leadership", "excerpt": "Alice Smith, Director"}]},
        "annual_revenue": {"status": "confirmed", "value": "$50M", "sources": [
            {"url": "https://invented.example", "excerpt": "Revenue $50M"}]}}
    evidence = normalize_evidence(raw, [url])
    assert evidence["employment"]["status"] == "confirmed"
    assert evidence["annual_revenue"]["status"] == "unclear"
    assert not evidence["annual_revenue"]["sources"]
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("https://user:pass@company.example") is None


def test_missing_or_conflicting_employment_is_unclear_not_former():
    result = _merge([{"person_verified": True}], [])
    assert result.employment_status == "unclear"
    assert result.person_verified is False
    checks = _validated_checks({"employee_count": {"verdict": "match", "requirement": "200+"}}, result.evidence)
    assert checks["employee_count"]["verdict"] == "unclear"
    assert checks["employment"]["verdict"] == "unclear"
    assert proof_rows({}, {}) == []


def test_identity_normalization_and_missing_name_deduplication():
    one = identity_fields({"contact_name": " ALICE Smith ", "company_name": "Acme, Inc.",
        "email_address": " Alice@Example.COM ", "linkedin_profile_url": "http://www.linkedin.com/in/alice/?trk=upload"})
    two = identity_fields({"contact_name": "alice smith", "company_name": "Acme Inc",
        "email_address": "alice@example.com", "linkedin_profile_url": "https://linkedin.com/in/alice"})
    assert one == two
    rows, stats = deduplicate(pd.DataFrame({"Contact Name": ["", "", None], "Company Name": ["Acme", "Acme", "Acme"]}))
    assert len(rows) == 3
    assert stats["duplicates_removed"] == 0


def test_batch_database_lookup_500_existing_300_uploaded(db):
    from app.utils.lead_identity import assign_identities
    original = Job(id=str(uuid.uuid4()), status="completed", total_rows=500)
    db.add(original)
    db.flush()
    records = [{"id": str(uuid.uuid4()), "job_id": original.id,
                    "contact_name": f"Person {i}", "company_name": "Acme",
                    "email_address": f"person{i}@example.com", "processing_status": "completed"}
               for i in range(500)]
    assign_identities(db, records)
    db.bulk_insert_mappings(Lead, records)
    db.commit()
    uploads = [{"id": str(uuid.uuid4()), "contact_name": f"Person {i}", "company_name": "Acme",
                    "email_address": f"person{i}@example.com"} for i in range(300, 600)]
    queries = []
    def count_sql(connection, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", count_sql)
    try:
        assign_identities(db, uploads)
    finally:
        event.remove(db.bind, "before_cursor_execute", count_sql)
    previous_ids = {record["tracking_id"] for record in records}
    assert sum(row["tracking_id"] in previous_ids for row in uploads) == 200
    assert sum(row["tracking_id"] not in previous_ids for row in uploads) == 100
    assert len(queries) <= 2  # Batch lookup, not one round trip per input row.


def test_import_redelivery_does_not_duplicate_rows(db, monkeypatch):
    from contextlib import contextmanager

    from app.tasks import import_task
    job = Job(id=str(uuid.uuid4()), status="importing")
    db.add(job)
    db.commit()
    @contextmanager
    def session():
        yield db
        db.commit()
    monkeypatch.setattr(import_task, "db_session", session)
    monkeypatch.setattr(import_task, "_download_blob", lambda _: b"test")
    monkeypatch.setattr(import_task, "parse_excel", lambda *a, **kw: pd.DataFrame({
        "Contact Name": ["Alice", "Alice", "Bob"], "Company Name": ["Acme"] * 3,
        "Email Address": ["alice@example.com", "alice@example.com", "bob@example.com"]}))
    import_task.import_file.run(job.id, "test.xlsx", ".xlsx", "test.xlsx")
    assert job.total_rows == 2
    assert job.duplicates_removed == 1
    assert job.status == "queued"
    import_task.import_file.run(job.id, "test.xlsx", ".xlsx", "test.xlsx")
    assert db.query(Lead).filter_by(job_id=job.id).count() == 2


def test_changed_icp_worker_uses_saved_research_and_new_qualification(db, monkeypatch):
    from app.scoring.scoring_engine import ScoreResult
    from app.validators.contact_verifier import ContactVerifyResult
    from app.validators.icp_validator import ICPResult
    add_lead(db, completed=True)
    lead = add_lead(db, icp="Different ICP")
    calls = []
    def qualify(*args, **kwargs):
        calls.append("icp")
        assert kwargs["evidence"]
        assert args[1] == "Different ICP"
        return ICPResult(icp_score=4, raw_output='{"checks": {}}')
    def unexpected(*args, **kwargs):
        pytest.fail("Saved research was researched or scraped again")
    monkeypatch.setattr(tasks, "research_lead", unexpected)
    monkeypatch.setattr(tasks, "persist_and_dispatch_embed", unexpected)
    monkeypatch.setattr(tasks, "CompanyLogger", lambda **kw: tasks._NullLog())
    monkeypatch.setattr(tasks, "validate_icp", qualify)
    monkeypatch.setattr(tasks, "verify_contact", lambda *a: ContactVerifyResult())
    monkeypatch.setattr(tasks, "score_lead", lambda *a, **kw: ScoreResult(total_score=40))
    tasks._process_lead(SimpleNamespace(), db, lead.id, lead.job.icp_prompt)
    db.commit()
    db.expire_all()
    assert calls == ["icp"]
    assert lead.final_score.total_score == 40
    assert lead.reuse_kind == "research"
    assert lead.job.research_reused_rows == 1
    assert _lead_to_response(lead).evidence_checked_at == "2026-01-01T00:00:00+00:00"


def test_export_tracking_and_formula_like_notes_are_safe(db):
    import openpyxl
    from app.export.result_builder import (
        ALL_COLUMNS,
        _build_csv,
        _build_excel,
        _fetch_rows,
    )
    lead = add_lead(db, completed=True)
    lead.tracking.notes, lead.tracking.reviewed = '=HYPERLINK("https://bad.example")', True
    rows = _fetch_rows(lead.job_id, db)
    assert rows[0]["Reviewed"] == "Yes"
    assert rows[0]["ICP Input"] == lead.job.icp_prompt
    workbook = openpyxl.load_workbook(io.BytesIO(_build_excel(rows)))
    cell = workbook.active.cell(3, ALL_COLUMNS.index("Tracking Notes") + 1)
    assert cell.data_type == "s"
    assert cell.value.startswith("'=")
    assert "'=HYPERLINK" in _build_csv(rows).decode("utf-8-sig")
