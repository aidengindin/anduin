"""Route tests via FastAPI TestClient.

DB-free: the ``get_conn`` dependency is overridden to a dummy and the query
functions are monkeypatched, so these assert routing, rendering and JSON shape
without a live database. TestClient is used WITHOUT its context manager so the
pool-opening lifespan never runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from anduin.config import AppConfig, FileConfig, Secrets
from anduin.web import queries
from anduin.web.app import create_app
from anduin.web.deps import get_conn


def _config() -> AppConfig:
    return AppConfig(
        secrets=Secrets(database_url="postgresql://u:p@localhost/anduin"),
        file=FileConfig(),
    )


@pytest.fixture()
def client() -> TestClient:
    app = create_app(_config())
    app.dependency_overrides[get_conn] = lambda: object()  # never touched by patched queries
    return TestClient(app)


def _dt(y, mo, d, h=0):
    return datetime(y, mo, d, h, tzinfo=timezone.utc)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_dashboard_renders(client, monkeypatch):
    # Patch the whole queries.home() bundle — home page uses sleep-hero design.
    monkeypatch.setattr(
        queries, "home",
        lambda conn: {
            "sleep": None,
            "hrv": None,
            "rhr": None,
            "weight": None,
            "body_fat": None,
            "steps": {"value": 7654},
            "step_goal": 10000,
            "hrv_status": None,
            "rhr_status": None,
            "pmc": None,
            "bp": None,
            "workouts": [],
        },
    )
    r = client.get("/")
    assert r.status_code == 200
    # The home template renders a greeting and a sleep hero card.
    assert "Good" in r.text  # Good morning / Good afternoon / Good evening
    assert "Sleep" in r.text
    assert "7,654" in r.text
    assert "/ 10,000" not in r.text


def test_metrics_page_lists_all_metrics(client, monkeypatch):
    # Patch metric_index so the route doesn't need a real DB connection.
    def _fake_index(conn):
        groups: dict = {"recovery": [], "body": [], "activity": []}
        for key, m in queries.METRICS.items():
            groups[m["group"]].append({
                "key": key, "label": m["label"], "desc": m["desc"], "unit": m["unit"],
                "color": m["color"], "group": m["group"], "digits": m["digits"],
                "value": None, "delta": None, "dir": "flat", "spark": "",
            })
        return groups

    monkeypatch.setattr(queries, "metric_index", _fake_index)
    r = client.get("/metrics")
    assert r.status_code == 200
    for meta in queries.METRICS.values():
        assert meta["label"] in r.text


def test_metric_json_ok(client, monkeypatch):
    payload = {"metric": "steps", "label": "Steps", "unit": "count", "bucket": "1 hour", "points": []}
    monkeypatch.setattr(queries, "metric_series", lambda conn, m, s, e: payload)
    r = client.get("/api/metrics/steps.json?since=2026-07-01&until=2026-07-02")
    assert r.status_code == 200 and r.json() == payload


def test_metric_json_unknown_404(client):
    r = client.get("/api/metrics/bogus.json")
    assert r.status_code == 404


def test_prometheus_metrics_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        queries, "ingest_freshness",
        lambda conn: [
            {"source": "google_health", "last_ingest_epoch": 1_700_000_000, "lag_seconds": 42},
        ],
    )
    r = client.get("/-/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'anduin_source_ingest_lag_seconds{source="google_health"} 42' in r.text


def test_workouts_list_renders(client, monkeypatch):
    monkeypatch.setattr(
        queries, "list_workouts",
        lambda conn, s, e, sport=None: [
            {"source": "intervals", "activity_uid": "a1", "sport": "cycling", "program": None,
             "device": "wahoo", "started_at": _dt(2026, 7, 1, 8), "ended_at": _dt(2026, 7, 1, 9),
             "duration_s": 3600, "calories": 700.0, "calories_is_derived": False,
             "training_load": 55.0, "steps": None, "steps_is_derived": False},
        ],
    )
    monkeypatch.setattr(queries, "sports", lambda conn, s, e: ["cycling", "running"])
    r = client.get("/workouts")
    assert r.status_code == 200
    assert "cycling" in r.text
    assert "/workouts/intervals/a1" in r.text


def test_workout_detail_renders(client, monkeypatch):
    detail = {
        "header": {"source": "liftosaur", "activity_uid": "w9", "sport": "strength", "program": "GZCLP",
                   "device": None, "started_at": _dt(2026, 7, 1, 18), "ended_at": None, "duration_s": None,
                   "calories": None, "calories_is_derived": False, "training_load": None,
                   "steps": None, "steps_is_derived": False},
        "streams": {},
        "exercises": [
            {"name": "Squat", "idx": 0, "is_unilateral": False,
             "sets": [{"set_index": 0, "weight_kg": 100.0, "reps": 5, "left_reps": None, "rpe": 8.0, "is_warmup": False}]},
        ],
    }
    monkeypatch.setattr(queries, "workout_detail", lambda conn, src, uid: detail)
    r = client.get("/workouts/liftosaur/w9")
    assert r.status_code == 200
    assert "Squat" in r.text and "GZCLP" in r.text


def test_workout_detail_404(client, monkeypatch):
    monkeypatch.setattr(queries, "workout_detail", lambda conn, src, uid: None)
    r = client.get("/workouts/x/y")
    assert r.status_code == 404


def test_workout_detail_shows_pounds_and_warmups_first(client, monkeypatch):
    # queries._load_strength already orders warmups first; the template must
    # render that order as-is and display the stored kg as lb.
    detail = {
        "header": {"source": "liftosaur", "activity_uid": "w9", "sport": "strength", "program": "GZCLP",
                   "device": None, "started_at": _dt(2026, 7, 1, 18), "ended_at": None, "duration_s": None,
                   "calories": None, "calories_is_derived": False, "training_load": None,
                   "steps": None, "steps_is_derived": False},
        "streams": {},
        "exercises": [
            {"name": "Squat", "idx": 0, "is_unilateral": False, "sets": [
                {"set_index": 3, "weight_kg": 11.34, "reps": 5, "left_reps": None, "rpe": None, "is_warmup": True},
                {"set_index": 0, "weight_kg": 54.431, "reps": 8, "left_reps": None, "rpe": None, "is_warmup": False},
            ]},
        ],
    }
    monkeypatch.setattr(queries, "workout_detail", lambda conn, src, uid: detail)
    r = client.get("/workouts/liftosaur/w9")
    assert r.status_code == 200
    assert "25 lb" in r.text and "120 lb" in r.text
    assert " kg" not in r.text
    assert r.text.index("25 lb") < r.text.index("120 lb")  # warmup rendered first
