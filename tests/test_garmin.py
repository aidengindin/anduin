"""Garmin Personal API extractor: pure emitters + auth/extract wiring.

Fixtures are shaped from the published Swagger DTOs; swap in real payloads once
a key has been exercised against the live API.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import respx

from anduin.config import AppConfig, FileConfig, Secrets
from anduin.sources import garmin
from anduin.sources.garmin import _hrv_rows, _resting_hr_rows, _sleep_rows, _summary_rows

# 2026-10-20 23:30 -> 2026-10-21 07:15 EDT (UTC-4), as epoch ms.
_START_GMT = int(datetime(2026, 10, 21, 3, 30, tzinfo=timezone.utc).timestamp() * 1000)
_END_GMT = int(datetime(2026, 10, 21, 11, 15, tzinfo=timezone.utc).timestamp() * 1000)
_END_LOCAL = int(datetime(2026, 10, 21, 7, 15, tzinfo=timezone.utc).timestamp() * 1000)

NIGHT = {
    "id": 1729481400000,
    "calendarDate": "2026-10-21",
    "sleepTimeSeconds": 25200,
    "sleepStartTimestampGMT": _START_GMT,
    "sleepEndTimestampGMT": _END_GMT,
    "sleepEndTimestampLocal": _END_LOCAL,
    "deepSleepSeconds": 5400,
    "lightSleepSeconds": 13200,
    "remSleepSeconds": 6600,
    "awakeSleepSeconds": 2700,
    "awakeCount": 3,
    "averageSpO2Value": 95.0,
    "lowestSpO2Value": 89.0,
    "highestSpO2Value": 99.0,
}


def test_summary_rows_take_daily_steps():
    rows = _summary_rows({"data": [{
        "calendarDate": "2026-10-20",
        "includesWellnessData": True,
        "movement": {"steps": {"goal": 10000, "value": 8421}},
    }]})
    assert len(rows) == 1
    r = rows[0]
    assert (r["metric"], r["value"], r["unit"]) == ("steps_daily_total", 8421.0, "count")
    assert r["local_date"] == date(2026, 10, 20)
    assert r["source"] == "garmin"


def test_summary_rows_skip_days_without_wellness_data():
    """An unworn day must stay absent, not land as a 0 that activity_daily would
    publish as the day's total."""
    rows = _summary_rows({"data": [
        {"calendarDate": "2026-10-19", "includesWellnessData": False,
         "movement": {"steps": {"value": 0}}},
        {"calendarDate": "2026-10-20", "includesWellnessData": True, "movement": {}},
    ]})
    assert rows == []


def test_resting_hr_null_safe():
    d = date(2026, 10, 20)
    assert _resting_hr_rows({"restingHeartRate": None}, d) == []
    assert _resting_hr_rows(None, d) == []
    [r] = _resting_hr_rows({"restingHeartRate": 51}, d)
    # Shares the Air's metric name on purpose: canonical.daily_metrics
    # arbitrates it in favour of google_health.
    assert (r["metric"], r["value"], r["local_date"]) == ("resting_heart_rate", 51.0, d)


def test_sleep_row_is_a_stage_less_session_in_utc():
    session, spo2 = _sleep_rows(NIGHT)
    assert session["source"] == "garmin"
    assert session["started_at"] == datetime(2026, 10, 21, 3, 30, tzinfo=timezone.utc)
    assert session["ended_at"] == datetime(2026, 10, 21, 11, 15, tzinfo=timezone.utc)
    assert session["tz_offset_minutes"] == -240
    assert session["minutes_asleep"] == 420
    assert session["minutes_awake"] == 45
    assert session["minutes_in_sleep_period"] == 465
    assert session["is_main_sleep"] is True
    assert session["summary"]["deepSleepSeconds"] == 5400
    assert session["session_uid"] == "1729481400000"

    by_metric = {r["metric"]: r for r in spo2}
    assert by_metric["spo2_nightly_avg"]["value"] == 95.0
    assert by_metric["spo2_nightly_min"]["value"] == 89.0
    # Keyed to the local wake date, like canonical.spo2_daily.
    assert {r["local_date"] for r in spo2} == {date(2026, 10, 21)}


def test_sleep_row_accepts_iso_timestamps():
    night = {**NIGHT,
             "sleepStartTimestampGMT": "2026-10-21T03:30:00.0",
             "sleepEndTimestampGMT": "2026-10-21T11:15:00.0",
             "sleepEndTimestampLocal": "2026-10-21T07:15:00.0"}
    session, _ = _sleep_rows(night)
    assert session["started_at"] == datetime(2026, 10, 21, 3, 30, tzinfo=timezone.utc)
    assert session["tz_offset_minutes"] == -240


def test_sleep_row_skips_empty_nights():
    assert _sleep_rows({**NIGHT, "sleepTimeSeconds": 0}) == (None, [])
    assert _sleep_rows({**NIGHT, "sleepStartTimestampGMT": None}) == (None, [])


def test_hrv_rows():
    rows = _hrv_rows({"hrvSummaries": [
        {"calendarDate": "2026-10-21", "lastNightAvg": 48, "weeklyAvg": 50},
        {"calendarDate": "2026-10-20", "lastNightAvg": None},
    ]})
    assert [(r["metric"], r["value"], r["local_date"]) for r in rows] == [
        ("hrv_nightly", 48.0, date(2026, 10, 21))]


def test_emitters_never_target_raw_samples():
    """The fallback design rests on Garmin never reaching raw.samples: every
    sample-grained view reads it without a source filter. Daily rows carry
    local_date (raw.daily_metrics), never valid_from (raw.samples)."""
    daily = (
        _summary_rows({"data": [{"calendarDate": "2026-10-20", "includesWellnessData": True,
                                 "movement": {"steps": {"value": 1}}}]})
        + _resting_hr_rows({"restingHeartRate": 50}, date(2026, 10, 20))
        + _sleep_rows(NIGHT)[1]
        + _hrv_rows({"hrvSummaries": [{"calendarDate": "2026-10-21", "lastNightAvg": 48}]})
    )
    assert daily
    for r in daily:
        assert "local_date" in r and "valid_from" not in r


def _app(key="k"):
    return AppConfig(secrets=Secrets(database_url="postgresql://x", garmin_api_key=key),
                     file=FileConfig())


def test_missing_key_is_an_error():
    res = garmin.extract(httpx.Client(), None, _app(key=""), date(2026, 10, 20),
                         date(2026, 10, 20), dry_run=True)
    assert res.errors and "GARMIN_API_KEY" in res.errors[0]


@respx.mock
def test_expired_key_reports_clearly():
    respx.post(garmin.TOKEN_URL).mock(return_value=httpx.Response(401, json={}))
    with httpx.Client() as http:
        res = garmin.extract(http, None, _app(), date(2026, 10, 20), date(2026, 10, 20),
                             dry_run=True)
    assert len(res.errors) == 1
    assert "rejected or expired" in res.errors[0]


@respx.mock
def test_dry_run_pulls_every_endpoint_with_bearer_token():
    token = respx.post(garmin.TOKEN_URL).mock(
        return_value=httpx.Response(200, text="tok", headers={"content-type": "text/plain"}))
    routes = [
        respx.get(f"{garmin.BASE}/usersummary-service/v2/dailysummaries").mock(
            return_value=httpx.Response(200, json={"data": []})),
        respx.get(url__startswith=f"{garmin.BASE}/hrv-service/hrv/daily/").mock(
            return_value=httpx.Response(200, json={"hrvSummaries": []})),
        respx.get(f"{garmin.BASE}/sleep-service/sleep/dailySleepSummariesByDate").mock(
            return_value=httpx.Response(200, json=[NIGHT])),
        respx.get(f"{garmin.BASE}/wellness-service/wellness/dailyHeartRate").mock(
            return_value=httpx.Response(200, json={"restingHeartRate": 50})),
    ]
    with httpx.Client() as http:
        res = garmin.extract(http, None, _app(), date(2026, 10, 20), date(2026, 10, 21),
                             dry_run=True)
    assert res.errors == []
    assert token.calls.last.request.headers["Authorization"] == "ApiKey k"
    for r in routes:
        assert r.called
        assert r.calls.last.request.headers["Authorization"] == "Bearer tok"
    # one dailyHeartRate call per day in the window
    assert routes[3].call_count == 2
