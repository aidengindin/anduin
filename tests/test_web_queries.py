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


def test_imperial_body_metrics_convert_values_and_trends():
    weight = queries.METRICS["body_weight"]
    assert weight["unit"] == "lb"
    assert queries._value_expr(weight) == "(value * 2.2046226218)"

    conn = FakeConn([{"slope_per_week": 1.0}])
    assert queries._body_slope_per_week(conn, "body_weight") == pytest.approx(2.2046226218)


def test_activity_metrics_expose_total_neat_and_workout():
    for base in ("steps", "active_calories"):
        assert "kind = 'total'" in queries.METRICS[base]["where"]
        assert "kind = 'neat'" in queries.METRICS[f"{base}_neat"]["where"]
        assert "kind = 'workout'" in queries.METRICS[f"{base}_workout"]["where"]


@pytest.mark.parametrize(
    "row,expected",
    [
        (None, "pending"),
        ({"base_n": 10, "elevated": False}, "pending"),
        ({"base_n": 20, "elevated": False}, "normal"),
        ({"base_n": 20, "elevated": True}, "elevated"),
    ],
)
def test_respiratory_rate_status(row, expected):
    conn = FakeConn([row])
    assert queries._metric_status(conn, "respiratory_rate") == expected


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
    out = queries.metric_series(conn, "resting_heart_rate", since, until)
    assert out["bucket"] == expected


@pytest.mark.parametrize("since,until", [
    (date(2026, 7, 1), date(2026, 7, 2)),
    (date(2026, 6, 1), date(2026, 7, 1)),
])
def test_counter_metrics_pin_the_bucket_to_a_day(since, until):
    # activity_daily is a daily rollup; an hour bucket would zero-fill 23
    # phantom points per real day.
    out = queries.metric_series(FakeConn([[]]), "steps", since, until)
    assert out["bucket"] == "1 day"


# test_daily_summary_merges_four_queries_by_day was removed — daily_summary()
# was a planned aggregation helper that was superseded by queries.home() in the
# current sleep-hero home design. See queries.home() for the current approach.


def test_list_workouts_maps_header_and_flags():
    rows = [
        {
            "source": "intervals", "activity_uid": "a1", "sport": "cycling", "program": None,
            "device": "wahoo", "started_at": _dt(2026, 7, 1, 8), "ended_at": _dt(2026, 7, 1, 9),
            "duration_s": 3600.0, "calories": 700.0, "calories_is_derived": True, "calories_method": "work",
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
         "duration_s": 3600.0, "calories": None, "calories_is_derived": False, "calories_method": None,
         "training_load": None, "steps": None, "steps_is_derived": False},
    ]
    out = queries.list_workouts(FakeConn([rows]), date(2026, 7, 1), date(2026, 7, 2))
    assert out[0]["source_url"] == "https://intervals.icu/activities/i9"


def test_workout_detail_groups_streams_and_strength():
    header = {
        "source": "liftosaur", "activity_uid": "w9", "sport": "strength", "program": "GZCLP",
        "device": None, "started_at": _dt(2026, 7, 1, 18), "ended_at": None,
        "duration_s": None, "calories": None, "calories_is_derived": False, "calories_method": None,
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


def test_strength_sql_orders_warmups_before_working_sets():
    # Liftohistory lists warmups after the working sets, so set_index alone
    # renders them last; the ordering has to come from the query.
    cur = FakeCursor([[]])
    queries._load_strength(cur, "liftosaur", "w9")
    sql = " ".join(cur.executed[0][0].split())
    assert "ORDER BY e.exercise_idx, s.is_warmup DESC, s.set_index" in sql


# --- zero-fill: counters vs point-in-time metrics ---------------------------

COUNTERS = ["steps", "steps_neat", "steps_workout",
            "active_calories", "active_calories_neat", "active_calories_workout"]
POINT_IN_TIME = ["body_weight", "body_fat_ratio", "muscle_mass", "fat_free_mass",
                 "hrv", "resting_heart_rate", "spo2", "skin_temp"]


@pytest.mark.parametrize("key", COUNTERS)
def test_counters_are_zero_filled(key):
    assert queries.METRICS[key].get("zero_fill") is True


@pytest.mark.parametrize("key", POINT_IN_TIME)
def test_point_in_time_metrics_are_not_zero_filled(key):
    # A day with no weigh-in is not a zero-pound day; carrying the last
    # reading forward is correct for these.
    assert queries.METRICS[key].get("zero_fill") is not True


def test_counter_latest_fills_days_and_ignores_the_metrics_own_filter():
    conn = FakeConn([{"latest": 0.0, "latest_at": date(2026, 7, 14), "baseline": 900.0}])
    out = queries._latest(conn, queries.METRICS["steps_workout"])
    sql, params = conn._cursor.executed[0]
    assert "generate_series" in sql
    assert params == {"days": 30}
    # The fill horizon must not carry the kind filter — that filtered max is
    # the stale "last day with a workout" this fix exists to stop reporting.
    horizon = sql.split("generate_series(")[1].split("- make_interval")[0]
    assert "kind" not in horizon
    assert out["value"] == 0.0 and out["at"] == date(2026, 7, 14)


def test_counter_series_zero_fills_buckets_up_to_the_horizon():
    conn = FakeConn([[{"t": _dt(2026, 7, 14), "avg": 0.0, "min": 0.0, "max": 0.0}]])
    out = queries.metric_series(conn, "steps_workout", date(2026, 7, 1), date(2026, 7, 15))
    sql, _ = conn._cursor.executed[0]
    assert "generate_series" in sql and "coalesce" in sql and "least" in sql
    assert out["points"][0]["avg"] == 0.0


def test_point_in_time_series_keeps_gaps():
    conn = FakeConn([[]])
    queries.metric_series(conn, "body_weight", date(2026, 7, 1), date(2026, 7, 15))
    sql, _ = conn._cursor.executed[0]
    assert "generate_series" not in sql


def test_counter_detail_stats_span_calendar_days_not_readings():
    conn = FakeConn([
        {"latest": 0.0, "latest_at": date(2026, 7, 14), "baseline": 300.0},  # _latest
        [{"v": 0.0}, {"v": 2100.0}],                                        # _daily_values
        {"avg7": 300.0, "lo": 0.0, "hi": 2100.0},                           # stats
        [{"d": date(2026, 7, 14), "v": 0.0}, {"d": date(2026, 7, 13), "v": 2100.0}],
    ])
    out = queries.metric_detail(conn, "steps_workout")
    stats_sql, stats_params = conn._cursor.executed[2]
    recent_sql, recent_params = conn._cursor.executed[3]
    assert stats_params == {"days": 7} and recent_params == {"days": 8}
    assert "LIMIT 7" not in stats_sql and "LIMIT 8" not in recent_sql
    assert out["lo"] == 0.0
    assert out["recent"][0] == {"d": date(2026, 7, 14), "v": 0.0}


def test_skin_temp_reads_the_daily_derivation_view():
    m = queries.METRICS["skin_temp"]
    assert m["view"] == "canonical.skin_temp_daily"
    assert (m["date_col"], m["value_col"]) == ("local_date", "variation_c")
