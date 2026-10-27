"""Pause/resume preserves durable progress and fences stale Celery deliveries."""
from contextlib import contextmanager, nullcontext
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import create_access_token
from app.models import FinalScore, Job, Lead
from app.tasks import dispatch_task, tasks


@pytest.fixture
def job(db):
    job = Job(id=str(uuid4()), status="running", total_rows=2, processed_rows=0,
              icp_prompt="Software companies", processing_mode="fresh",
              created_at=datetime.now(), dispatch_complete=True,
              dispatch_row_index=1, dispatch_lead_id=str(uuid4()))
    db.add(job)
    db.flush()
    for index in range(2):
        db.add(Lead(id=str(uuid4()), job_id=job.id, row_index=index))
    db.commit()
    return job


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(main, "request_dispatch", lambda *a, **kw: None)
    main.app.dependency_overrides[main.get_db] = lambda: db
    client = TestClient(main.app, base_url="http://localhost", headers={
        "Authorization": "Bearer " + create_access_token("admin"),
    })
    yield client
    client.close()
    main.app.dependency_overrides.pop(main.get_db, None)


def test_pause_resume_endpoints_preserve_policy_progress_and_reset_dispatch(client, db, job):
    job.processed_rows = 1
    db.commit()
    for _ in range(2):
        response = client.post(f"/jobs/{job.id}/pause")
        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        assert response.json()["processed_rows"] == 1
    assert client.get(f"/jobs/{job.id}/status").json()["status"] == "paused"
    assert client.post(f"/jobs/{job.id}/icp", json={
        "icp_prompt": "A different ICP", "processing_mode": "reuse",
    }).status_code == 409
    response = client.post(f"/jobs/{job.id}/resume")
    assert response.status_code == 200
    assert response.json()["processing_mode"] == "fresh"
    assert response.json()["icp_prompt"] == "Software companies"
    assert response.json()["processed_rows"] == 1
    assert job.dispatch_lead_id is None and job.dispatch_row_index is None
    assert not job.dispatch_complete
    assert client.post(f"/jobs/{job.id}/resume").status_code == 200


@pytest.mark.parametrize("state", ["queued", "importing", "completed", "failed", "import_failed"])
def test_invalid_transitions_are_rejected(client, db, job, state):
    job.status = state
    db.commit()
    assert client.post(f"/jobs/{job.id}/pause").status_code == 409
    assert client.post(f"/jobs/{job.id}/resume").status_code == 409
    assert job.status == state


def test_missing_job_and_authentication(client):
    assert client.post(f"/jobs/{uuid4()}/pause").status_code == 404
    assert client.post(f"/jobs/{uuid4()}/resume").status_code == 404
    client.headers.pop("Authorization")
    assert client.post(f"/jobs/{uuid4()}/pause").status_code == 401
    assert client.post(f"/jobs/{uuid4()}/resume").status_code == 401


def test_resume_intent_survives_broker_outage(client, db, job, monkeypatch):
    job.status = "paused"
    db.commit()
    def unavailable(*args, **kwargs):
        raise ConnectionError("broker unavailable")
    monkeypatch.setattr(main, "request_dispatch", unavailable)
    assert client.post(f"/jobs/{job.id}/resume").json()["status"] == "running"
    db.expire_all()
    assert db.get(Job, job.id).status == "running"
    assert not job.dispatch_complete


def test_pause_acknowledges_pending_deliveries_without_work_then_resume_finishes(client, db, job, monkeypatch):
    leads = db.query(Lead).filter_by(job_id=job.id).order_by(Lead.row_index).all()
    # Simulate a persisted score from a stage commit before the completion guard.
    for lead in leads:
        db.add(FinalScore(lead_id=lead.id, total_score=80, priority="High"))
    db.commit()
    monkeypatch.setattr(tasks, "reusable_lead", lambda *args: pytest.fail("No paid pipeline should start"))
    assert client.post(f"/jobs/{job.id}/pause").status_code == 200
    for lead in leads:
        tasks._process_lead(SimpleNamespace(), db, lead.id, job.icp_prompt)
    assert all(lead.processing_status == "pending" for lead in leads)
    assert job.processed_rows == 0
    assert client.delete(f"/jobs/{job.id}").status_code == 409
    assert client.post(f"/jobs/{job.id}/resume").status_code == 200
    for _ in range(2):  # Duplicate delivery must not duplicate progress or scores.
        for lead in leads:
            tasks._process_lead(SimpleNamespace(), db, lead.id, job.icp_prompt)
        db.commit()
    db.refresh(job)
    assert job.processed_rows == 2 and job.failed_rows == 0
    assert db.query(FinalScore).count() == 2


def test_inflight_completion_and_stale_finalizer_cannot_unpause(client, db, job, monkeypatch):
    @contextmanager
    def session():
        yield db
        db.commit()
    monkeypatch.setattr(tasks, "db_session", session)
    assert client.post(f"/jobs/{job.id}/pause").status_code == 200
    leads = db.query(Lead).filter_by(job_id=job.id).all()
    tasks._mark_lead_finished(db, leads[0], failed=False)
    db.commit()
    tasks.finalise_job.run([], job.id)
    db.refresh(job)
    assert job.status == "paused" and job.processed_rows == 1
    tasks._mark_lead_finished(db, leads[1], failed=False)
    db.commit()
    tasks.finalise_job.run([], job.id)
    assert job.status == "paused"
    assert client.post(f"/jobs/{job.id}/resume").status_code == 200
    assert job.dispatch_complete
    tasks.finalise_job.run([], job.id)
    assert job.status == "completed"


def test_dispatch_ignores_paused_job_and_republishes_only_unfinished_rows(client, db, job, monkeypatch):
    @contextmanager
    def session():
        yield db
        db.commit()
    monkeypatch.setattr(dispatch_task, "db_session", session)
    monkeypatch.setattr(dispatch_task, "broker_epoch", lambda: "test-epoch")
    monkeypatch.setattr(dispatch_task.celery, "producer_or_acquire", lambda: nullcontext(None))
    monkeypatch.setattr(dispatch_task.dispatch_job, "apply_async", lambda *a, **kw: None)
    published = []
    monkeypatch.setattr(tasks.process_lead, "apply_async", lambda **kw: published.append(kw["args"][0]))
    lead = db.query(Lead).filter_by(job_id=job.id).first()
    tasks._mark_lead_finished(db, lead, failed=False)
    db.commit()
    assert client.post(f"/jobs/{job.id}/pause").status_code == 200
    dispatch_task.dispatch_job.run(job.id)
    assert published == []
    assert client.post(f"/jobs/{job.id}/resume").status_code == 200
    dispatch_task.dispatch_job.run(job.id)
    assert len(published) == 1 and published[0] != lead.id
