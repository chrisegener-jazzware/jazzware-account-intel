"""Tests for the health-digest FastAPI router (JAZ-180).

Exercises /health/accounts/scored, /health/am, /health/am/{am}/scores, and
/health/am/{am}/digest by patching `health.api.run_pipeline` so we do not
touch HubSpot. Behaviour we lock in:

- accounts/scored returns the full portfolio worst-first
- am map groups by account_manager and falls back to 'unassigned@jazzware.com'
- am/{am}/scores filters to a single AM and stays empty (not 404) when none
- am/{am}/digest returns text/markdown by default, JSON envelope when fmt=json
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from health import api as health_api  # noqa: E402
from health.scoring import HealthScore  # noqa: E402


def _mk_score(
    *,
    cid: str,
    name: str,
    score: int,
    flag: str,
    am: str | None,
) -> HealthScore:
    return HealthScore(
        customer_id=cid,
        customer_name=name,
        account_manager=am,
        score=score,
        flag=flag,
        components={"tickets": 50, "integrations": 50, "open_load": 50, "ttr": 50, "escalation": 50},
        narrative=f"{name} narrative",
        signals={
            "tickets": {"total": 1, "open": 0, "high_priority": 0, "avg_time_to_close_hours": 1},
            "integrations": {"integration_count": 1, "error_rate_7d": 0.0, "failing_integrations": []},
        },
    )


@pytest.fixture
def sample_scores() -> list[HealthScore]:
    return [
        _mk_score(cid="1", name="Hotel A (red)", score=25, flag="red", am="alice@jazzware.com"),
        _mk_score(cid="2", name="Hotel B (yellow)", score=55, flag="yellow", am="alice@jazzware.com"),
        _mk_score(cid="3", name="Hotel C (green)", score=85, flag="green", am="bob@jazzware.com"),
        _mk_score(cid="4", name="Hotel D (unassigned)", score=70, flag="yellow", am=None),
    ]


@pytest.fixture
def client(monkeypatch, sample_scores):
    # Patch the pipeline so the API never calls HubSpot.
    def fake_pipeline(**_kw):
        return sample_scores

    monkeypatch.setattr(health_api, "run_pipeline", fake_pipeline)

    # Build a minimal app rather than importing the full account_intel app
    # (which would require all the DB + rollup deps).
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(health_api.router)
    return TestClient(app)


def test_accounts_scored_sorts_worst_first(client):
    r = client.get("/health/accounts/scored")
    assert r.status_code == 200
    data = r.json()
    assert [d["customer_id"] for d in data] == ["1", "2", "4", "3"]
    assert data[0]["flag"] == "red"


def test_am_map_groups_and_falls_back_to_unassigned(client):
    r = client.get("/health/am")
    assert r.status_code == 200
    data = r.json()
    assert "alice@jazzware.com" in data
    assert "bob@jazzware.com" in data
    assert "unassigned@jazzware.com" in data
    alice = [c["customer_id"] for c in data["alice@jazzware.com"]]
    assert alice == ["1", "2"]  # worst-first


def test_am_scores_filters_and_returns_empty_for_unknown_am(client):
    r = client.get("/health/am/alice@jazzware.com/scores")
    assert r.status_code == 200
    ids = [d["customer_id"] for d in r.json()]
    assert ids == ["1", "2"]

    r2 = client.get("/health/am/nobody@jazzware.com/scores")
    assert r2.status_code == 200
    assert r2.json() == []


def test_am_digest_renders_markdown_by_default(client):
    r = client.get("/health/am/alice@jazzware.com/digest", params={"use_claude": False})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    assert "Customer Health Digest" in body
    assert "alice@jazzware.com" in body
    assert "Hotel A (red)" in body


def test_am_digest_json_envelope(client):
    r = client.get(
        "/health/am/alice@jazzware.com/digest",
        params={"fmt": "json", "use_claude": False},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["am_email"] == "alice@jazzware.com"
    assert payload["count"] == 2
    assert "Customer Health Digest" in payload["markdown"]
    assert [s["customer_id"] for s in payload["scores"]] == ["1", "2"]


def test_single_score(client, sample_scores, monkeypatch):
    # Pipeline with a customer_id returns just that one in real flow; emulate it.
    def fake_one(*, customer_id=None, **_kw):
        return [s for s in sample_scores if s.customer_id == customer_id]

    monkeypatch.setattr(health_api, "run_pipeline", fake_one)
    r = client.get("/health/score/3", params={"use_claude": False})
    assert r.status_code == 200
    assert r.json()["customer_name"] == "Hotel C (green)"

    r2 = client.get("/health/score/does-not-exist", params={"use_claude": False})
    assert r2.status_code == 404
