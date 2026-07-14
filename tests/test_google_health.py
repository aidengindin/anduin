"""Emitter tests for the Google Health API v4 extractor.

Pure-function tests over the documented v4 dataPoint JSON shapes (camelCase,
int64-as-string, structured {year,month,day} dates). No HTTP, no DB — the list
client + upserts are covered elsewhere and by the manual verification pass.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from anduin.sources.google_health import (
    _emit_active_energy,
    _emit_distance,
    _emit_heart_rate,
    _emit_hrv_daily,
    _emit_hrv_sample,
    _emit_resting_heart_rate,
    _emit_respiratory_rate,
    _emit_sleep,
    _emit_spo2_daily,
    _emit_spo2_sample,
    _emit_steps,
    _filter_expr,
)

UTC = timezone.utc


# --- sleep session + stages ---


def _sleep_dp():
    return {
        "name": "users/me/dataTypes/sleep/dataPoints/NIGHT1",
        "sleep": {
            "interval": {
                "startTime": "2026-03-15T23:10:00Z",
                "endTime": "2026-03-16T07:00:00Z",
            },
            "type": "STAGES",
            "stages": [
                {"startTime": "2026-03-15T23:10:00Z", "endTime": "2026-03-15T23:40:00Z", "type": "LIGHT"},
                {"startTime": "2026-03-15T23:40:00Z", "endTime": "2026-03-16T00:30:00Z", "type": "DEEP"},
            ],
            "summary": {
                "minutesAsleep": "420",
                "minutesAwake": "30",
                "minutesInSleepPeriod": "480",
                "minutesToFallAsleep": "15",
                "minutesAfterWakeUp": "10",
                "efficiency": 90,
            },
        },
    }


def test_emit_sleep_session_header():
    session, _stages = _emit_sleep(_sleep_dp())
    assert session["source"] == "google_health"
    assert session["session_uid"] == "users/me/dataTypes/sleep/dataPoints/NIGHT1"
    assert session["started_at"] == datetime(2026, 3, 15, 23, 10, tzinfo=UTC)
    assert session["ended_at"] == datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
    assert session["sleep_type"] == "STAGES"
    # int64-as-string fields are coerced to ints
    assert session["minutes_asleep"] == 420
    assert session["minutes_awake"] == 30
    assert session["minutes_in_sleep_period"] == 480
    assert session["minutes_to_fall_asleep"] == 15
    assert session["minutes_after_wakeup"] == 10
    assert session["efficiency"] == 90.0
    assert session["natural_key"] == "sleep|users/me/dataTypes/sleep/dataPoints/NIGHT1"
    assert session["raw"]["sleep"]["type"] == "STAGES"
    # startTime is 'Z' (UTC) here, so the wearer's local offset is unknown.
    assert session["tz_offset_minutes"] is None


def test_emit_sleep_captures_local_tz_offset():
    dp = _sleep_dp()
    # A local-offset startTime (e.g. US Eastern DST) is captured in minutes.
    dp["sleep"]["interval"]["startTime"] = "2026-03-15T23:10:00-04:00"
    session, _ = _emit_sleep(dp)
    assert session["tz_offset_minutes"] == -240


def test_emit_sleep_stage_segments():
    _session, stages = _emit_sleep(_sleep_dp())
    assert len(stages) == 2
    assert stages[0]["stage"] == "LIGHT"
    assert stages[0]["started_at"] == datetime(2026, 3, 15, 23, 10, tzinfo=UTC)
    assert stages[0]["ended_at"] == datetime(2026, 3, 15, 23, 40, tzinfo=UTC)
    assert stages[1]["stage"] == "DEEP"
    # every stage carries the parent session_uid for the FK-style key
    assert {s["session_uid"] for s in stages} == {"users/me/dataTypes/sleep/dataPoints/NIGHT1"}


def test_emit_sleep_missing_summary_and_stages_is_tolerant():
    dp = {"name": "users/me/dataTypes/sleep/dataPoints/N2",
          "sleep": {"interval": {"startTime": "2026-03-15T23:10:00Z",
                                 "endTime": "2026-03-16T07:00:00Z"}, "type": "CLASSIC"}}
    session, stages = _emit_sleep(dp)
    assert stages == []
    assert session["minutes_asleep"] is None
    assert session["efficiency"] is None
    assert session["sleep_type"] == "CLASSIC"


# --- fine-grained spo2 / hrv samples ---


def test_emit_spo2_sample():
    dp = {"oxygenSaturation": {"sampleTime": {"physicalTime": "2026-03-16T02:00:00Z"},
                               "percentage": 97.0}}
    row = _emit_spo2_sample(dp)
    assert row["metric"] == "spo2"
    assert row["value"] == 97.0
    assert row["unit"] == "%"
    assert row["valid_from"] == datetime(2026, 3, 16, 2, 0, tzinfo=UTC)
    assert row["valid_to"] == datetime(2026, 3, 16, 2, 0, tzinfo=UTC)
    assert row["natural_key"] == "spo2|2026-03-16T02:00:00+00:00"


def test_emit_hrv_sample():
    dp = {"heartRateVariability": {
        "sampleTime": {"physicalTime": "2026-03-16T02:05:00Z"},
        "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 42.0}}
    row = _emit_hrv_sample(dp)
    assert row["metric"] == "hrv"
    assert row["value"] == 42.0
    assert row["unit"] == "ms"
    assert row["valid_from"] == datetime(2026, 3, 16, 2, 5, tzinfo=UTC)


# --- daily scalars keyed on local_date ---


def test_emit_resting_heart_rate_daily():
    dp = {"dailyRestingHeartRate": {
        "date": {"year": 2026, "month": 3, "day": 15},
        "beatsPerMinute": "58",
        "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"}}}
    row = _emit_resting_heart_rate(dp)
    assert row["metric"] == "resting_heart_rate"
    assert row["value"] == 58.0
    assert row["unit"] == "bpm"
    assert row["local_date"] == date(2026, 3, 15)
    assert row["natural_key"] == "resting_heart_rate|2026-03-15"


def test_emit_respiratory_rate_daily():
    dp = {"dailyRespiratoryRate": {
        "date": {"year": 2026, "month": 3, "day": 15},
        "breathsPerMinute": 14.2}}
    row = _emit_respiratory_rate(dp)
    assert row["metric"] == "respiratory_rate"
    assert row["value"] == 14.2
    assert row["unit"] == "br/min"
    assert row["local_date"] == date(2026, 3, 15)


def test_emit_spo2_daily_emits_three_rows():
    dp = {"dailyOxygenSaturation": {
        "date": {"year": 2026, "month": 3, "day": 15},
        "average": 96.0, "minimum": 92.0, "maximum": 99.0}}
    rows = _emit_spo2_daily(dp)
    by_metric = {r["metric"]: r for r in rows}
    assert set(by_metric) == {"spo2_daily_avg", "spo2_daily_min", "spo2_daily_max"}
    assert by_metric["spo2_daily_avg"]["value"] == 96.0
    assert by_metric["spo2_daily_min"]["value"] == 92.0
    assert by_metric["spo2_daily_max"]["value"] == 99.0
    assert all(r["local_date"] == date(2026, 3, 15) for r in rows)
    # distinct natural keys so all three survive the upsert
    assert len({r["natural_key"] for r in rows}) == 3


def test_emit_hrv_daily():
    dp = {"dailyHeartRateVariability": {
        "date": {"year": 2026, "month": 3, "day": 15},
        "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 45.0}}
    row = _emit_hrv_daily(dp)
    assert row["metric"] == "hrv_daily_rmssd"
    assert row["value"] == 45.0
    assert row["unit"] == "ms"
    assert row["local_date"] == date(2026, 3, 15)


# --- AIP-160 filter expressions (per record kind; verified live against v4) ---


def test_filter_expr_sleep_uses_interval_end_time_rfc3339():
    # Sleep sessions filter on end_time (not start_time) — the "special handling".
    f = _filter_expr("sleep", "session", date(2026, 7, 6), date(2026, 7, 12))
    assert f == (
        'sleep.interval.end_time >= "2026-07-06T00:00:00Z" '
        'AND sleep.interval.end_time < "2026-07-13T00:00:00Z"'
    )


def test_filter_expr_interval_uses_start_time_rfc3339():
    f = _filter_expr("steps", "interval", date(2026, 7, 6), date(2026, 7, 12))
    assert f == (
        'steps.interval.start_time >= "2026-07-06T00:00:00Z" '
        'AND steps.interval.start_time < "2026-07-13T00:00:00Z"'
    )


def test_filter_expr_sample_uses_physical_time_rfc3339():
    f = _filter_expr("oxygen_saturation", "sample", date(2026, 7, 6), date(2026, 7, 12))
    assert f.startswith(
        'oxygen_saturation.sample_time.physical_time >= "2026-07-06T00:00:00Z"'
    )


def test_filter_expr_daily_uses_civil_date_no_time():
    # Daily summaries need a civil date (YYYY-MM-DD), not an RFC3339 timestamp.
    f = _filter_expr("daily_resting_heart_rate", "daily", date(2026, 7, 6), date(2026, 7, 12))
    assert f == (
        'daily_resting_heart_rate.date >= "2026-07-06" '
        'AND daily_resting_heart_rate.date < "2026-07-13"'
    )
    assert "T00:00:00Z" not in f


# --- intraday heart_rate / steps / distance / active_energy -> raw.samples ---
# NOTE: these v4 payload shapes are inferred (proto field names confirmed, JSON
# envelope not seen live). See the module NOTE in google_health.py.


def test_emit_heart_rate_sample():
    dp = {"heartRate": {"sampleTime": {"physicalTime": "2026-03-16T08:00:00Z"},
                        "beatsPerMinute": "62"}}
    row = _emit_heart_rate(dp)
    assert row["metric"] == "heart_rate"
    assert row["value"] == 62.0
    assert row["unit"] == "bpm"
    assert row["valid_from"] == datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
    assert row["valid_to"] == datetime(2026, 3, 16, 8, 0, tzinfo=UTC)


def test_emit_steps_interval():
    dp = {"steps": {"interval": {"startTime": "2026-03-16T08:00:00Z",
                                 "endTime": "2026-03-16T08:01:00Z"},
                    "count": "120"}}
    row = _emit_steps(dp)
    assert row["metric"] == "steps"
    assert row["value"] == 120.0
    assert row["unit"] == "count"
    # interval types span the bucket, not a point
    assert row["valid_from"] == datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
    assert row["valid_to"] == datetime(2026, 3, 16, 8, 1, tzinfo=UTC)


def test_emit_distance_converts_meters_to_km():
    dp = {"distance": {"interval": {"startTime": "2026-03-16T08:00:00Z",
                                    "endTime": "2026-03-16T08:01:00Z"},
                       "meters": 1500.0}}
    row = _emit_distance(dp)
    assert row["metric"] == "distance"
    assert row["value"] == 1.5  # v4 gives meters; stored as km for continuity
    assert row["unit"] == "km"


def test_emit_active_energy_keeps_legacy_metric_name():
    dp = {"activeEnergyBurned": {"interval": {"startTime": "2026-03-16T08:00:00Z",
                                              "endTime": "2026-03-16T08:01:00Z"},
                                 "kcal": 8.5}}
    row = _emit_active_energy(dp)
    # metric MUST stay 'active_energy' — canonical.neat_energy/total_energy key on it
    assert row["metric"] == "active_energy"
    assert row["value"] == 8.5
    assert row["unit"] == "kcal"
