"""Row-mapping tests for the web query layer.

These exercise the Python-side shaping (row -> dict, day merge, stream/strength
grouping) without a live TimescaleDB, via a scripted fake cursor that returns
canned rows in call order. The SQL itself is validated end-to-end by the manual
verification pass against the real database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from anduin.web import queries


class FakeCursor:
    """Returns queued results in order; one entry consumed per fetchall/fetchone."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def _next(self):
        r = self._results[self._i]
        self._i += 1
        return r

    def fetchall(self):
        return self._next()

    def fetchone(self):
        return self._next()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, results):
        self._cursor = FakeCursor(results)

    def cursor(self):
        return self._cursor


def _dt(y, mo, d, h=0):
    return datetime(y, mo, d, h, tzinfo=timezone.utc)


def test_metric_series_shapes_points_and_epoch():
    rows = [
        {"t": _dt(2026, 7, 1, 0), "avg": 60.0, "min": 55.0, "max": 70.0},
        {"t": _dt(2026, 7, 1, 1), "avg": None, "min": None, "max": None},
    ]
    conn = FakeConn([rows])
    out = queries.metric_series(conn, "resting_heart_rate", date(2026, 7, 1), date(2026, 7, 2))
    assert out["metric"] == "resting_heart_rate"
    assert out["unit"] == "bpm"
    assert out["points"][0] == {"t": int(_dt(2026, 7, 1).timestamp()), "avg": 60.0, "min": 55.0, "max": 70.0}
    assert out["points"][1]["avg"] is None


def test_metric_series_unknown_metric_raises():
    with pytest.raises(KeyError):
        queries.metric_series(FakeConn([]), "nonsense", date(2026, 7, 1), date(2026, 7, 2))


@pytest.mark.parametrize(
    "since,until,expected",
    [
        (date(2026, 7, 1), date(2026, 7, 2), "1 minute"),
        (date(2026, 6, 1), date(2026, 7, 1), "1 hour"),
        (date(2026, 1, 1), date(2026, 7, 1), "1 day"),
    ],
)
def test_bucket_widens_with_range(since, until, expected):
    conn = FakeConn([[]])
    out = queries.metric_series(conn, "steps", since, until)
    assert out["bucket"] == expected


# test_daily_summary_merges_four_queries_by_day was removed — daily_summary()
# was a planned aggregation helper that was superseded by queries.home() in the
# current sleep-hero home design. See queries.home() for the current approach.


def test_list_workouts_maps_header_and_flags():
    rows = [
        {
            "source": "intervals", "activity_uid": "a1", "sport": "cycling", "program": None,
            "device": "wahoo", "started_at": _dt(2026, 7, 1, 8), "ended_at": _dt(2026, 7, 1, 9),
            "duration_s": 3600.0, "calories": 700.0, "calories_is_derived": True,
            "training_load": 55.0, "steps": None, "steps_is_derived": False,
        }
    ]
    conn = FakeConn([rows])
    out = queries.list_workouts(conn, date(2026, 7, 1), date(2026, 7, 2))
    assert len(out) == 1
    w = out[0]
    assert w["sport"] == "cycling"
    assert w["duration_s"] == 3600
    assert w["calories"] == 700.0 and w["calories_is_derived"] is True
    assert w["steps"] is None


def test_source_url_intervals_and_none_for_others():
    assert queries._source_url("intervals", "i123") == "https://intervals.icu/activities/i123"
    assert queries._source_url("liftosaur", "lift1") is None


def test_list_workouts_includes_source_url():
    rows = [
        {"source": "intervals", "activity_uid": "i9", "sport": "Ride", "program": None,
         "device": None, "started_at": _dt(2026, 7, 1, 8), "ended_at": _dt(2026, 7, 1, 9),
         "duration_s": 3600.0, "calories": None, "calories_is_derived": False,
         "training_load": None, "steps": None, "steps_is_derived": False},
    ]
    out = queries.list_workouts(FakeConn([rows]), date(2026, 7, 1), date(2026, 7, 2))
    assert out[0]["source_url"] == "https://intervals.icu/activities/i9"


def test_workout_detail_groups_streams_and_strength():
    header = {
        "source": "liftosaur", "activity_uid": "w9", "sport": "strength", "program": "GZCLP",
        "device": None, "started_at": _dt(2026, 7, 1, 18), "ended_at": None,
        "duration_s": None, "calories": None, "calories_is_derived": False,
        "training_load": None, "steps": None, "steps_is_derived": False,
    }
    streams = []  # no cardio streams for pure strength
    strength = [
        {"exercise_uid": "w9/0", "exercise_name": "Squat", "exercise_idx": 0, "is_unilateral": False,
         "set_index": 0, "weight_kg": 100.0, "reps": 5, "left_reps": None, "rpe": 8.0, "is_warmup": False},
        {"exercise_uid": "w9/0", "exercise_name": "Squat", "exercise_idx": 0, "is_unilateral": False,
         "set_index": 1, "weight_kg": 100.0, "reps": 5, "left_reps": None, "rpe": 8.5, "is_warmup": False},
        {"exercise_uid": "w9/1", "exercise_name": "Split Squat", "exercise_idx": 1, "is_unilateral": True,
         "set_index": 0, "weight_kg": 20.0, "reps": 8, "left_reps": 7, "rpe": None, "is_warmup": False},
    ]
    conn = FakeConn([header, streams, strength])
    out = queries.workout_detail(conn, "liftosaur", "w9")
    assert out["header"]["sport"] == "strength"
    assert out["streams"] == {}
    assert [e["name"] for e in out["exercises"]] == ["Squat", "Split Squat"]
    assert len(out["exercises"][0]["sets"]) == 2
    assert out["exercises"][1]["is_unilateral"] is True
    assert out["exercises"][1]["sets"][0]["left_reps"] == 7


def test_workout_detail_not_found_returns_none():
    conn = FakeConn([None])  # header fetchone -> None
    assert queries.workout_detail(conn, "x", "y") is None
