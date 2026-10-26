import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app import main
from app.auth import create_access_token


@pytest.mark.parametrize("driver_args,expected_status", [
    (("42000", "Client IP is not allowed. (40615)"), 503),
    (("08001", "Connection failed"), 503),
    (("HYT00", "Connection timed out"), 503),
    (("42000", "Duplicate ORDER BY column (169)"), 500),
])
def test_database_errors_keep_cors_and_hide_driver_details(monkeypatch, driver_args, expected_status):
    def fail(*args):
        raise DBAPIError("SELECT private_data", {}, Exception(*driver_args))

    monkeypatch.setattr(main, "_get_job_or_404", fail)
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.get("/jobs/707ea9a5-1e57-4af4-ab5c-de7a7a02f9c4/status", headers={
            "Authorization": "Bearer " + create_access_token("admin"),
            "Origin": "http://localhost:5174",
        })
    assert response.status_code == expected_status
    assert response.headers["access-control-allow-origin"] == "http://localhost:5174"
    assert "SELECT" not in response.text
    assert "40615" not in response.text
    assert "private_data" not in response.text
