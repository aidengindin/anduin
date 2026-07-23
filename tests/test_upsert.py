"""Unit tests for the sleep + daily-metric upsert helpers.

These exercise the Python-side param shaping (Jsonb wrapping, empty handling,
one-transaction semantics) via a fake connection/cursor that records calls,
mirroring the approach in test_web_queries. The SQL itself is validated by the
manual verification pass against the real database.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from anduin.upsert import upsert_daily_metrics, upsert_samples, upsert_sleep


class FakeCursor:
    def __init__(self):
        self.executed = []      # list of (sql, params)
        self.executed_many = []  # list of (sql, seq)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, seq):
        self.executed_many.append((sql, list(seq)))


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


# --- upsert_daily_metrics ---


def test_upsert_daily_metrics_empty_is_noop():
    conn = FakeConn()
    assert upsert_daily_metrics(conn, 1, []) == 0
    assert conn.cur.executed_many == []
    assert conn.commits == 0


def test_upsert_daily_metrics_wraps_raw_and_returns_count():
    conn = FakeConn()
    rows = [
        {"source": "google_health", "device": "fitbit_air", "recording_method": "device",
         "metric": "resting_heart_rate", "value": 58.0, "unit": "bpm",
         "local_date": "2026-03-15", "tz_offset_minutes": None,
         "raw": {"beatsPerMinute": "58"}, "natural_key": "resting_heart_rate|2026-03-15"},
    ]
    n = upsert_daily_metrics(conn, 7, rows)
    assert n == 1
    assert conn.commits == 1
    assert len(conn.cur.executed_many) == 1
    _sql, params = conn.cur.executed_many[0]
    assert isinstance(params[0]["raw"], Jsonb)
    # owner id is stamped on every row
    assert params[0]["user_id"] == 7
    # scalar fields pass through untouched
    assert params[0]["metric"] == "resting_heart_rate"
    assert params[0]["local_date"] == "2026-03-15"


# --- upsert_samples ---


def test_upsert_samples_stamps_user_id_on_every_row():
    conn = FakeConn()
    rows = [
        {"source": "google_health", "device": "fitbit_air", "recording_method": "device",
         "metric": "steps", "value": 12.0, "unit": "count",
         "valid_from": "f", "valid_to": "t", "natural_key": "steps|f",
         "raw": {"count": 12}},
    ]
    n = upsert_samples(conn, 3, rows)
    assert n == 1
    _sql, params = conn.cur.executed_many[0]
    assert params[0]["user_id"] == 3
    assert isinstance(params[0]["raw"], Jsonb)


# --- upsert_sleep ---


def _session_row(summary):
    return {
        "source": "google_health", "session_uid": "abc", "device": "fitbit_air",
        "recording_method": "device", "started_at": "s", "ended_at": "e",
        "is_main_sleep": True, "sleep_type": "STAGES",
        "minutes_asleep": 420, "minutes_awake": 30, "minutes_in_sleep_period": 480,
        "minutes_to_fall_asleep": 15, "minutes_after_wakeup": 10, "efficiency": 90.0,
        "summary": summary, "raw": {"sleep": {}}, "natural_key": "sleep|abc",
    }


def test_upsert_sleep_wraps_raw_and_summary():
    conn = FakeConn()
    stages = [{"source": "google_health", "session_uid": "abc", "stage": "DEEP",
               "started_at": "s", "ended_at": "e"}]
    upsert_sleep(conn, 1, _session_row({"stagesSummary": []}), stages)
    assert conn.commits == 1
    assert len(conn.cur.executed) == 1  # one header execute
    _sql, hp = conn.cur.executed[0]
    assert hp["user_id"] == 1
    assert isinstance(hp["raw"], Jsonb)
    assert isinstance(hp["summary"], Jsonb)
    assert len(conn.cur.executed_many) == 1  # stages executemany
    # each stage row is stamped with the owner id, other fields preserved
    assert conn.cur.executed_many[0][1] == [{**stages[0], "user_id": 1}]


def test_upsert_sleep_null_summary_stays_none():
    conn = FakeConn()
    upsert_sleep(conn, 1, _session_row(None), [])
    _sql, hp = conn.cur.executed[0]
    assert hp["summary"] is None
    assert isinstance(hp["raw"], Jsonb)


def test_upsert_sleep_no_stages_skips_executemany():
    conn = FakeConn()
    upsert_sleep(conn, 1, _session_row(None), [])
    assert conn.cur.executed_many == []
    assert conn.commits == 1
