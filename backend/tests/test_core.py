import pandas as pd
from app.auth import create_access_token, verify_access_token
from app.main import app
from app.scoring.scoring_engine import _parse_response
from app.utils.deduplicator import deduplicate
from fastapi.testclient import TestClient


def test_signed_token_round_trip():
    assert verify_access_token(create_access_token("admin"))["sub"] == "admin"


def test_public_health_and_protected_jobs():
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/health/live").status_code == 200
    assert client.get("/jobs").status_code == 401


def test_openapi_exposes_outreach_intelligence():
    client = TestClient(app, base_url="http://localhost")
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["LeadResultResponse"]["properties"]
    assert "outreach_intelligence" in properties


def test_dedup_is_lossless_for_similar_distinct_names():
    frame = pd.DataFrame({
        "CRM Contact ID": ["", "", ""],
        "Email Address": ["", "", "same@example.com"],
        "Contact Name": ["Person 1", "Person 10", "Person 1"],
        "Company Name": ["Acme", "Acme", "Acme"],
    })
    result, stats = deduplicate(frame)
    assert list(result["Contact Name"]) == ["Person 1", "Person 10"]
    assert stats["duplicates_removed"] == 1


def test_backend_computes_weighted_total_and_serializes_outreach():
    result = _parse_response({
        "icp_score": 80,
        "authority_score": 70,
        "data_score": 60,
        "growth_score": 50,
        "contact_score": 40,
        "reasoning": "Evidence-backed summary",
        "growth_signals": "Recent hiring",
        "outreach_intelligence": {"recommended_channel": "email"},
    })
    assert result.total_score == 66.0
    assert '"recommended_channel": "email"' in result.outreach_intelligence
