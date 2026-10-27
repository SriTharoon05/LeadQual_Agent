"""No paid calls, usage attempts or ingestion retries may start for paused jobs."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.admin import billing, telemetry
from app.models import CompanyScrape, Job, Lead, ValidationResult
from app.tasks import embed_task, tasks
from app.utils import job_pause
from app.vectorstore import embedder


@pytest.fixture
def workload(db, monkeypatch):
    job = Job(id=str(uuid4()), status="paused", processing_mode="fresh", total_rows=1,
              icp_prompt="Software companies", created_at=datetime.now())
    db.add(job)
    db.flush()
    lead = Lead(id=str(uuid4()), job_id=job.id, row_index=0)
    db.add(lead)
    db.flush()
    row = CompanyScrape(id=str(uuid4()), job_id=job.id, lead_id=lead.id,
                        ingest_status="embedding", ingest_attempts=0,
                        clean_text="Saved company website text", scraped_at=datetime.now())
    db.add(row)
    db.commit()

    @contextmanager
    def session(*args, **kwargs):
        yield db
    monkeypatch.setattr(job_pause, "SessionLocal", session)
    monkeypatch.setattr(embed_task, "SessionLocal", session)
    monkeypatch.setattr(embed_task, "db_session", session)
    monkeypatch.setattr(embed_task, "locked_lead_session", session)
    telemetry.clear_context()
    yield job, lead, row
    telemetry.clear_context()


@pytest.fixture
def paid_spy(monkeypatch):
    records, calls = [], []
    monkeypatch.setattr(billing, "before_model_call", lambda: None)
    monkeypatch.setattr(billing, "load_settings", lambda: {"version": 1, "pricing": {
        "model": "gpt-4.1-mini", "input_per_million": .4, "cached_per_million": .1,
        "output_per_million": 1.6, "embedding_model": "text-embedding-ada-002",
        "embedding_per_million": .1, "search_per_thousand": 14,
    }})
    monkeypatch.setattr(billing, "enqueue", lambda kind, data, **kw: records.append((kind, data)))
    return records, calls


@pytest.mark.parametrize("kind,web", [("embedding", False), ("llm", False), ("llm", True)])
def test_paused_calls_are_blocked_before_sdk_and_usage_journal(workload, paid_spy, kind, web):
    job, lead, _ = workload
    records, calls = paid_spy
    for _ in range(5):
        with pytest.raises(job_pause.JobPaused):
            with billing.metered_call(stage="web", module="test", kind=kind, web=web,
                    request_model="test", job_id=job.id, lead_id=lead.id):
                calls.append("paid request")
    assert calls == records == []


def test_implicit_task_context_cannot_bypass_pause(workload, paid_spy):
    job, lead, _ = workload
    telemetry.set_context(job_id=job.id, lead_id=lead.id)
    with pytest.raises(job_pause.JobPaused):
        with billing.metered_call(stage="embedding", module="test", request_model="test"):
            pytest.fail("Paid call was permitted")
    assert paid_spy[0] == []


def test_unavailable_status_fails_closed_before_journaling(workload, paid_spy, monkeypatch):
    job, _, _ = workload
    def unavailable():
        raise ConnectionError("SQL unavailable")
    monkeypatch.setattr(job_pause, "SessionLocal", unavailable)
    with pytest.raises(ConnectionError):
        with billing.metered_call(stage="web", module="test", request_model="test", job_id=job.id):
            pytest.fail("Unknown pause status authorized a charge")
    assert paid_spy[0] == []


def test_pause_between_embedding_batches_stops_next_paid_request(db, workload, paid_spy, monkeypatch):
    job, lead, _ = workload
    records, calls = paid_spy
    job.status = "running"
    db.commit()
    monkeypatch.setattr(embedder.settings, "EMBED_DIM", 2)

    def create(**kwargs):
        calls.append(kwargs)
        # Simulate another process pausing while the first paid call is in flight.
        job.status = "paused"
        db.commit()
        return SimpleNamespace(model="text-embedding-ada-002",
            usage=SimpleNamespace(prompt_tokens=len(kwargs["input"]), total_tokens=len(kwargs["input"])),
            data=[SimpleNamespace(index=i, embedding=[.1, .2]) for i in range(len(kwargs["input"]))])

    monkeypatch.setattr(embedder, "_client", lambda: SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    with pytest.raises(job_pause.JobPaused):
        embedder.embed(["Text"] * (embedder.EMBED_BATCH + 1), job_id=job.id, lead_id=lead.id)
    assert len(calls) == 1
    assert len(records) == 2  # The in-flight request's start and final usage only.
    for _ in range(3):
        with pytest.raises(job_pause.JobPaused):
            embedder.embed(["Text"], job_id=job.id, lead_id=lead.id)
    assert len(calls) == 1 and len(records) == 2


def test_pause_during_settings_load_is_seen_before_admitting_paid_request(db, workload, paid_spy, monkeypatch):
    job, _, _ = workload
    job.status = "running"
    db.commit()
    def settings_changed():
        job.status = "paused"
        db.commit()
        return None
    monkeypatch.setattr(billing, "load_settings", settings_changed)
    with pytest.raises(job_pause.JobPaused):
        with billing.metered_call(stage="web", module="test", request_model="test", job_id=job.id):
            pytest.fail("Paid call admitted using stale status")
    assert paid_spy[0] == []


@pytest.mark.parametrize("phase", ["queued", "scraping", "embedding"])
def test_queued_ingestion_deliveries_do_no_work_or_retry(db, workload, monkeypatch, phase):
    job, lead, row = workload
    row.ingest_status = phase
    db.commit()
    fail = lambda *a, **kw: pytest.fail("Paused task attempted work or scheduled retry")
    monkeypatch.setattr(embed_task, "research_company", fail)
    monkeypatch.setattr(embed_task, "embed", fail)
    monkeypatch.setattr(embed_task.ingest_company, "retry", fail)
    monkeypatch.setattr(embed_task.embed_company, "retry", fail)
    for _ in range(3):
        task = embed_task.embed_company if phase == "embedding" else embed_task.ingest_company
        task.run(row.id)
    assert row.ingest_attempts == 0
    assert row.ingest_status == phase
    assert job.processed_rows == 0 and lead.processing_status == "pending"


def test_recovery_and_direct_dispatch_skip_paused_then_resume(db, workload, monkeypatch):
    job, _, row = workload
    published = []
    monkeypatch.setattr(embed_task.embed_company, "apply_async", lambda **kw: published.append(kw))
    embed_task.recover_ingestion_requests()
    embed_task.dispatch_ingestion(row.id, force=True)
    assert published == [] and row.ingest_dispatched_at is None
    job.status = "running"
    db.commit()
    embed_task.recover_ingestion_requests()
    assert len(published) == 1
    assert published[0]["args"] == [row.id]


def test_pause_during_embedding_preserves_checkpoint_and_attempt_budget(db, workload, monkeypatch):
    job, _, row = workload
    job.status = "running"
    db.commit()
    def interrupted(*a, **kw):
        job.status = "paused"
        db.commit()
        raise job_pause.JobPaused()
    monkeypatch.setattr(embed_task, "embed", interrupted)
    monkeypatch.setattr(embed_task.embed_company, "retry", lambda *a, **kw: pytest.fail("Pause retried"))
    embed_task.embed_company.run(row.id)
    assert row.ingest_status == "embedding"
    assert row.ingest_attempts == 0
    assert row.clean_text == "Saved company website text"
    assert row.ingest_dispatched_at is None and row.ingest_error is None


def test_lock_retry_is_acknowledged_while_paused(workload):
    _, _, row = workload
    task = SimpleNamespace(retry=lambda **kw: pytest.fail("Paused ingestion retry scheduled"))
    assert embed_task._retry_ingestion(task, row.id, TimeoutError(), 10) is None


def test_lead_retry_drops_before_admission_for_paused_job(workload, monkeypatch):
    job, lead, _ = workload
    monkeypatch.setattr(tasks, "LeadAdmission", lambda: pytest.fail("Paused lead sought admission"))
    monkeypatch.setattr(tasks.process_lead, "retry", lambda **kw: pytest.fail("Paused lead retried"))
    tasks.process_lead.run(lead.id, job.icp_prompt)
    assert lead.processing_status == "pending" and job.failed_rows == 0


def test_deferred_embedding_resumes_and_is_not_repeated_after_completion(db, workload, monkeypatch):
    job, _, row = workload
    calls = []
    monkeypatch.setattr(embed_task, "embed", lambda chunks, **kw: calls.append(chunks) or [[.1]] * len(chunks))
    monkeypatch.setattr(embed_task, "upsert_chunks", lambda **kw: len(kw["chunks"]))
    embed_task.embed_company.run(row.id)
    assert calls == []
    job.status = "running"
    db.commit()
    embed_task.embed_company.run(row.id)
    assert row.ingest_status == "completed" and row.embedded == 1
    embed_task.embed_company.run(row.id)
    assert len(calls) == 1


def test_inflight_lead_stops_before_next_paid_stage_and_resumes_saved_evidence(db, workload, paid_spy, monkeypatch):
    from app.validators.web_researcher import WebResearchResult
    from app.validators.contact_verifier import ContactVerifyResult
    from app.validators.icp_validator import ICPResult
    from app.scoring.scoring_engine import ScoreResult
    job, lead, _ = workload
    job.status = "running"
    db.commit()

    @contextmanager
    def locked(*args, **kw):
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    monkeypatch.setattr(tasks, "locked_lead_session", locked)
    monkeypatch.setattr(tasks, "LeadAdmission", nullcontext)
    monkeypatch.setattr(tasks, "CompanyLogger", lambda **kw: tasks._NullLog())
    monkeypatch.setattr(tasks, "_set_cached_web", lambda *a: None)
    monkeypatch.setattr(tasks, "persist_and_dispatch_embed", lambda *a, **kw: None)
    monkeypatch.setattr(tasks.process_lead, "retry", lambda **kw: pytest.fail("Pause consumed a retry"))

    def in_flight_response(*args, **kw):
        job.status = "paused"
        db.commit()
        result = WebResearchResult(evidence={"industry": {"value": "Software"}},
                                   checked_at=datetime.now(timezone.utc).isoformat())
        result.raw_output = json.dumps(result.to_dict())
        return result
    contact = ContactVerifyResult()
    contact.raw_output = json.dumps(contact.to_dict())
    monkeypatch.setattr(tasks, "research_lead", in_flight_response)
    monkeypatch.setattr(tasks, "verify_contact", lambda *a, **kw: contact)
    tasks.process_lead.run(lead.id, job.icp_prompt)
    assert job.status == "paused" and lead.processing_status == "pending"
    assert job.failed_rows == 0
    assert {row.stage for row in db.query(ValidationResult).filter_by(lead_id=lead.id)} == {"web", "contact"}
    assert paid_spy[0] == []  # The subsequent actual ICP call was never journaled/sent.

    job.status = "running"
    db.commit()
    monkeypatch.setattr(tasks, "research_lead", lambda *a, **kw: pytest.fail("Paid web stage was repeated"))
    icp = ICPResult(checks={"industry": {"verdict": "match"}})
    icp.raw_output = json.dumps(icp.to_dict())
    monkeypatch.setattr(tasks, "validate_icp", lambda *a, **kw: icp)
    monkeypatch.setattr(tasks, "score_lead", lambda *a, **kw: ScoreResult(raw_output='{"completed":true}'))
    tasks.process_lead.run(lead.id, job.icp_prompt)
    db.refresh(job)
    assert lead.processing_status == "completed" and job.processed_rows == 1
