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

    conn = FakeConn([{"smoothed_slope_per_week": 1.0}])
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


# --- weight goal tracking --------------------------------------------------


def _goal(kind="bulk", target=0.4, started=date(2026, 7, 1)):
    return {"kind": kind, "target": target, "started_on": started}


def test_weight_goal_status_converts_the_smoothed_slope_to_pounds():
    conn = FakeConn([{"slope": 1.0, "n": 12, "days": 21.0}])
    out = queries.weight_goal_status(conn, _goal())
    assert out["rate"] == pytest.approx(2.2046226218)


def test_on_target_when_the_rate_is_inside_the_tolerance_band():
    conn = FakeConn([{"slope": 0.4 / 2.2046226218, "n": 12, "days": 21.0}])
    out = queries.weight_goal_status(conn, _goal(target=0.4))
    assert out["verdict"] == "on_target"
    assert (out["lo"], out["hi"]) == (pytest.approx(0.2), pytest.approx(0.6))


def test_gaining_faster_than_the_band_reads_too_fast():
    conn = FakeConn([{"slope": 1.0 / 2.2046226218, "n": 12, "days": 21.0}])
    assert queries.weight_goal_status(conn, _goal(target=0.4))["verdict"] == "too_fast"


def test_gaining_slower_than_the_band_reads_too_slow():
    conn = FakeConn([{"slope": 0.05 / 2.2046226218, "n": 12, "days": 21.0}])
    assert queries.weight_goal_status(conn, _goal(target=0.4))["verdict"] == "too_slow"


def test_a_cut_losing_too_slowly_is_not_mistaken_for_too_fast():
    # Target -1.0 lb/wk, band [-1.5, -0.5]; -0.2 is numerically above the band
    # but behind schedule, because the direction of progress is downward.
    conn = FakeConn([{"slope": -0.2 / 2.2046226218, "n": 12, "days": 21.0}])
    assert queries.weight_goal_status(conn, _goal(kind="cut", target=-1.0))["verdict"] == "too_slow"


def test_a_cut_losing_faster_than_intended_reads_too_fast():
    conn = FakeConn([{"slope": -2.0 / 2.2046226218, "n": 12, "days": 21.0}])
    assert queries.weight_goal_status(conn, _goal(kind="cut", target=-1.0))["verdict"] == "too_fast"


def test_maintain_uses_a_fixed_band_instead_of_a_collapsed_one():
    conn = FakeConn([{"slope": 0.1 / 2.2046226218, "n": 12, "days": 21.0}])
    out = queries.weight_goal_status(conn, _goal(kind="maintain", target=None))
    assert (out["lo"], out["hi"]) == (pytest.approx(-0.25), pytest.approx(0.25))
    assert out["verdict"] == "on_target"


@pytest.mark.parametrize(
    "row",
    [
        {"slope": 0.2, "n": 4, "days": 21.0},   # too few readings
        {"slope": 0.2, "n": 12, "days": 6.0},   # too short a span
        {"slope": None, "n": 0, "days": 0.0},   # nothing at all
    ],
)
def test_a_thin_window_is_pending_rather_than_a_wild_rate(row):
    out = queries.weight_goal_status(FakeConn([row]), _goal())
    assert out["pending"] is True and out["verdict"] is None


def test_the_fit_window_never_reaches_back_before_the_phase_started():
    conn = FakeConn([{"slope": 0.2, "n": 12, "days": 21.0}])
    queries.weight_goal_status(conn, _goal(started=date(2026, 8, 10)))
    _, params = conn._cursor.executed[0]
    assert params["phase_start"] == date(2026, 8, 10)


def test_a_none_tombstone_reports_the_rate_without_a_verdict():
    conn = FakeConn([{"slope": 0.2, "n": 12, "days": 21.0}])
    out = queries.weight_goal_status(conn, _goal(kind="none", target=None))
    assert out["rate"] is not None and out["verdict"] is None


# --- goal corridor ---------------------------------------------------------

_DAY = 86400
_T0 = int(_dt(2026, 7, 1).timestamp())


def _pts(n, avg7=180.0):
    return [{"t": _T0 + i * _DAY, "avg7": avg7} for i in range(n)]


def _anchors(points):
    return {p["t"]: p["avg7"] for p in points}


def test_corridor_anchors_four_weeks_back_at_the_target_rate():
    pts = _pts(29)
    lo, hi = queries._goal_corridor(pts, _anchors(pts), _goal(target=0.4, started=date(2026, 6, 1)))
    # 180 + (0.4 -/+ 0.2) * 4 weeks
    assert lo[28] == pytest.approx(180.8)
    assert hi[28] == pytest.approx(182.4)


def test_corridor_is_blank_for_the_first_four_weeks_of_a_phase():
    pts = _pts(29)
    lo, _ = queries._goal_corridor(pts, _anchors(pts), _goal(target=0.4, started=date(2026, 6, 1)))
    assert all(v is None for v in lo[:28])


def test_corridor_width_does_not_grow_with_phase_length():
    pts = _pts(200)
    lo, hi = queries._goal_corridor(pts, _anchors(pts), _goal(target=0.4, started=date(2026, 1, 1)))
    assert hi[40] - lo[40] == pytest.approx(hi[199] - lo[199])


def test_corridor_is_absent_before_the_phase_start_date():
    pts = _pts(60)
    lo, _ = queries._goal_corridor(pts, _anchors(pts), _goal(target=0.4, started=date(2026, 8, 1)))
    assert lo[40] is None


def test_maintain_corridor_is_a_flat_band_around_the_anchor():
    pts = _pts(29)
    lo, hi = queries._goal_corridor(pts, _anchors(pts), _goal(kind="maintain", target=None,
                                               started=date(2026, 6, 1)))
    assert lo[28] == pytest.approx(179.0) and hi[28] == pytest.approx(181.0)


def test_no_corridor_without_a_goal():
    assert queries._goal_corridor(_pts(29), _anchors(_pts(29)), None) == ([], [])


def test_no_corridor_for_a_tombstoned_phase():
    goal = _goal(kind="none", target=None, started=date(2026, 6, 1))
    assert queries._goal_corridor(_pts(29), _anchors(_pts(29)), goal) == ([], [])


def test_corridor_skips_days_whose_anchor_is_missing():
    pts = [p for i, p in enumerate(_pts(29)) if i != 0]
    lo, _ = queries._goal_corridor(pts, _anchors(pts), _goal(target=0.4, started=date(2026, 6, 1)))
    assert lo[-1] is None


# --- chart overlays --------------------------------------------------------


def test_body_weight_is_bucketed_daily_even_over_a_short_range():
    conn = FakeConn([[], []])
    out = queries.metric_series(conn, "body_weight", date(2026, 7, 1), date(2026, 7, 8))
    assert out["bucket"] == "1 day"


def test_rolling_averages_are_aligned_onto_the_reading_points():
    points = [{"t": _dt(2026, 7, 1), "avg": 81.6, "min": 81.6, "max": 81.6},
              {"t": _dt(2026, 7, 2), "avg": 81.7, "min": 81.7, "max": 81.7}]
    overlay = [{"t": _dt(2026, 7, 1), "avg7": 81.5, "avg30": 81.0}]
    out = queries.metric_series(FakeConn([points, overlay]), "body_weight",
                                date(2026, 7, 1), date(2026, 7, 3))
    assert out["points"][0]["avg7"] == pytest.approx(81.5 * 2.2046226218)
    assert out["points"][0]["avg30"] == pytest.approx(81.0 * 2.2046226218)
    assert out["points"][1]["avg7"] is None


def test_a_corridor_with_nothing_drawable_is_omitted_entirely():
    """A phase younger than four weeks has no anchor yet, so every day is None.
    Serializing that would light up the chart legend for a ribbon that is not
    there."""
    points = [{"t": _dt(2026, 7, 1), "avg": 81.6, "min": 81.6, "max": 81.6}]
    overlay = [{"t": _dt(2026, 7, 1), "avg7": 81.5, "avg30": 81.0}]
    goal = {"kind": "bulk", "target": 0.4, "started_on": date(2026, 6, 25)}
    out = queries.metric_series(FakeConn([points, overlay]), "body_weight",
                                date(2026, 7, 1), date(2026, 7, 2), goal)
    assert "goal" not in out


def test_corridor_spans_the_whole_range_using_anchors_from_before_it():
    """The anchor lives four weeks behind the day it draws, so a one-month
    chart needs trend rows from before the range start. Looking anchors up in
    the visible points alone left 28 of every 31 days blank."""
    points = [{"t": _dt(2026, 8, 1), "avg": 84.0, "min": 84.0, "max": 84.0},
              {"t": _dt(2026, 8, 2), "avg": 84.1, "min": 84.1, "max": 84.1}]
    # The overlay query reaches back before the range: 7/4 and 7/5 anchor 8/1 and 8/2.
    overlay = [{"t": _dt(2026, 7, 4), "avg7": 81.0, "avg30": 80.5},
               {"t": _dt(2026, 7, 5), "avg7": 81.1, "avg30": 80.6},
               {"t": _dt(2026, 8, 1), "avg7": 84.0, "avg30": 83.0},
               {"t": _dt(2026, 8, 2), "avg7": 84.1, "avg30": 83.1}]
    goal = {"kind": "bulk", "target": 0.4, "started_on": date(2026, 6, 1)}
    out = queries.metric_series(FakeConn([points, overlay]), "body_weight",
                                date(2026, 8, 1), date(2026, 8, 3), goal)
    assert all(v is not None for v in out["goal"]["lo"]), "every day in range should draw"
    assert out["goal"]["lo"][0] == pytest.approx(81.0 * 2.2046226218 + (0.4 - 0.2) * 4)


def test_the_overlay_query_reaches_back_before_the_range_start():
    conn = FakeConn([[], []])
    queries.metric_series(conn, "body_weight", date(2026, 8, 1), date(2026, 8, 3),
                          {"kind": "bulk", "target": 0.4, "started_on": date(2026, 6, 1)})
    _, params = conn._cursor.executed[1]
    assert params["start"].date() == date(2026, 7, 4)  # 28 days before the range
