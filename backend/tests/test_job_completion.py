from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.models import Lead
from app.tasks import tasks
from test_reuse_and_evidence import add_lead


def test_all_reused_job_finishes_without_new_token_costs(db, monkeypatch):
    add_lead(db, completed=True)
    target = add_lead(db)
    tasks._process_lead(SimpleNamespace(), db, target.id, target.job.icp_prompt)
    db.commit()

    @contextmanager
    def session():
        yield db
        db.commit()
    monkeypatch.setattr(tasks, "db_session", session)
    tasks.finalise_job.run([], target.job_id)
    assert target.job.status == "completed"
    assert target.job.processed_rows == 1
    assert target.job.reused_rows == 1
    assert target.job.total_tokens_used == 0


def test_last_failed_lead_does_not_leave_job_running(db, monkeypatch):
    target = add_lead(db)
    tasks._mark_lead_finished(db, target, failed=True)
    db.commit()

    @contextmanager
    def session():
        yield db
        db.commit()
    monkeypatch.setattr(tasks, "db_session", session)
    tasks.finalise_job.run([], target.job_id)
    assert target.job.status == "failed"
    assert target.job.failed_rows == target.job.processed_rows == 1


def test_worker_error_retries_then_marks_failure(db, monkeypatch):
    target = add_lead(db)
    @contextmanager
    def locked(_):
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    monkeypatch.setattr(tasks, "locked_lead_session", locked)
    monkeypatch.setattr(tasks, "_process_lead", lambda *args: (_ for _ in ()).throw(ValueError("Upstream unavailable")))
    result = tasks.process_lead.apply(args=(target.id, target.job.icp_prompt), headers={"pipeline_failures": 3})
    assert result.successful()
    assert db.get(Lead, target.id).processing_status == "failed"
    assert target.job.failed_rows == 1


def test_readiness_rejects_unmigrated_schema(monkeypatch):
    import redis
    from app import main
    from fastapi import HTTPException
    monkeypatch.setattr(main, "ping_db", lambda: True)
    monkeypatch.setattr(main, "schema_ready", lambda: False)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **kw: SimpleNamespace(ping=lambda: True))
    with pytest.raises(HTTPException) as error:
        main.readiness()
    assert error.value.status_code == 503
    assert error.value.detail["schema"] is False
