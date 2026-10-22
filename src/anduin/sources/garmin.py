"""Garmin Connect Personal API extractor — whole-day fallback health data.

Auth: a self-generated Personal API key (connect.garmin.com → Settings →
Personal API Token, 30d–1y expiry) is exchanged once per run for a 24h OAuth2
bearer token. There is no refresh token, so nothing is persisted to state_dir.

Garmin is a strictly *whole-day fallback* for the Fitbit Air: the canonical
layer (migration 0027) uses a Garmin value only for a day or night the Air has
nothing for. To make that hold by construction this extractor **never writes
raw.samples**. Every sample-grained view (the HR union, NEAT/total energy,
workout calories) reads raw.samples without a source filter, and
canonical.samples only arbitrates exact-timestamp ties, so Garmin samples would
interleave with or double-count against the Air. Everything here lands as a
daily scalar (raw.daily_metrics) or a stage-less sleep session.

  steps_daily_total      v2 daily summary movement.steps.value (no intraday)
  resting_heart_rate     dailyHeartRate.restingHeartRate (shares the Air's name;
                         canonical.daily_metrics prefers google_health)
  sleep session          dailySleepSummariesByDate: window + stage *totals*
                         only — the API exposes no stage timeline
  spo2_nightly_avg/min/max  from the same sleep summary, keyed to the wake date
  hrv_nightly            hrv daily summaries' lastNightAvg (ms)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import get_json
from anduin.sources.base import SourceResult
from anduin.upsert import upsert_daily_metrics, upsert_sleep

logger = logging.getLogger(__name__)

SOURCE = "garmin"
BASE = "https://personal.connectapi.garmin.com"
TOKEN_URL = f"{BASE}/personalapitoken-service/personal-api/personal-api-key/token"

# Server-side range caps from the Swagger spec.
_SLEEP_RANGE_DAYS = 31
_SUMMARY_RANGE_DAYS = 365


# --- parsing helpers ---------------------------------------------------------

def _num(v) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v) -> datetime | None:
    """Garmin timestamp -> aware datetime with the wall clock taken as UTC.

    The spec types these as date-time strings but describes them as "in ms",
    so accept both epoch milliseconds (number or numeric string) and ISO-8601.
    Garmin's ``*Local`` fields use the same encoding with the local wall clock
    in place of UTC, which is why the offset is derived as local - GMT."""
    if v is None or v == "":
        return None
    if isinstance(v, dict):  # ConnectTimestamp {"date": ...}
        return _ts(v.get("date"))
    n = _num(v)
    if n is not None:
        return datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date(v) -> date | None:
    if isinstance(v, dict):
        v = v.get("date")
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _chunks(since: date, until: date, days: int):
    start = since
    while start <= until:
        end = min(start + timedelta(days=days - 1), until)
        yield start, end
        start = end + timedelta(days=1)


def _daily_row(metric: str, value: float, unit: str, local_date: date, raw: dict,
               tz_offset_minutes: int | None = None) -> dict:
    return {
        "source": SOURCE,
        "device": None,
        "recording_method": "device",
        "metric": metric,
        "value": value,
        "unit": unit,
        "local_date": local_date,
        "tz_offset_minutes": tz_offset_minutes,
        "natural_key": f"{metric}|{local_date.isoformat()}",
        "raw": raw,
    }


# --- emitters (pure: payload -> row dicts) -----------------------------------

def _summary_rows(payload: dict | None) -> list[dict]:
    """v2 daily summaries -> steps_daily_total rows.

    A day Garmin has no wellness data for (watch not worn) must stay absent, not
    become a 0: activity_daily would otherwise publish that 0 as the day's total
    on exactly the days the fallback exists for."""
    rows = []
    for d in (payload or {}).get("data") or []:
        if not d.get("includesWellnessData"):
            continue
        local_date = _date(d.get("calendarDate"))
        steps = _num(((d.get("movement") or {}).get("steps") or {}).get("value"))
        if local_date is None or steps is None:
            continue
        rows.append(_daily_row("steps_daily_total", steps, "count", local_date, d))
    return rows


def _resting_hr_rows(payload: dict | None, local_date: date) -> list[dict]:
    """dailyHeartRate -> resting_heart_rate. Not v2's heartRate.restingValue,
    which the spec says falls back to the day's *minimum* HR when there is no
    resting value — a different quantity under the same name."""
    rhr = _num((payload or {}).get("restingHeartRate"))
    if rhr is None or rhr <= 0:
        return []
    return [_daily_row("resting_heart_rate", rhr, "bpm", local_date, payload or {})]


def _sleep_rows(night: dict) -> tuple[dict | None, list[dict]]:
    """One DailySleepDTO -> (raw.sleep_sessions row, spo2 daily rows).

    No stage rows: the API only gives per-stage totals, which go in ``summary``.
    SpO2 is keyed to the local wake date, matching canonical.spo2_daily."""
    secs = _num(night.get("sleepTimeSeconds"))
    started_at = _ts(night.get("sleepStartTimestampGMT"))
    ended_at = _ts(night.get("sleepEndTimestampGMT"))
    if not secs or started_at is None or ended_at is None or ended_at < started_at:
        return None, []

    local_end = _ts(night.get("sleepEndTimestampLocal"))
    tz_off = (round((local_end - ended_at).total_seconds() / 60)
              if local_end is not None else None)

    session_uid = str(night.get("id") or night.get("calendarDate") or started_at.isoformat())

    def _mins(key: str) -> int | None:
        v = _num(night.get(key))
        return round(v / 60) if v is not None else None

    summary = {k: night.get(k) for k in (
        "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds",
        "awakeSleepSeconds", "unmeasurableSleepSeconds", "awakeCount",
        "napTimeSeconds", "averageRespirationValue", "avgHeartRate", "avgSleepStress",
    ) if night.get(k) is not None}

    session = {
        "source": SOURCE,
        "session_uid": session_uid,
        "device": None,
        "recording_method": "device",
        "started_at": started_at,
        "ended_at": ended_at,
        "tz_offset_minutes": tz_off,
        "is_main_sleep": True,
        "sleep_type": "SUMMARY",
        "minutes_asleep": round(secs / 60),
        "minutes_awake": _mins("awakeSleepSeconds"),
        "minutes_in_sleep_period": round((ended_at - started_at).total_seconds() / 60),
        "minutes_to_fall_asleep": None,
        "minutes_after_wakeup": None,
        "efficiency": None,
        "summary": summary or None,
        "raw": night,
        "natural_key": f"sleep|{session_uid}",
    }

    wake_date = (local_end or ended_at).date()
    spo2 = []
    for metric, key in (("spo2_nightly_avg", "averageSpO2Value"),
                        ("spo2_nightly_min", "lowestSpO2Value"),
                        ("spo2_nightly_max", "highestSpO2Value")):
        v = _num(night.get(key))
        if v is not None and v > 0:
            spo2.append(_daily_row(metric, v, "%", wake_date, night, tz_off))
    return session, spo2


def _hrv_rows(payload: dict | None) -> list[dict]:
    rows = []
    for s in (payload or {}).get("hrvSummaries") or []:
        local_date = _date(s.get("calendarDate"))
        v = _num(s.get("lastNightAvg"))
        if local_date is None or v is None or v <= 0:
            continue
        rows.append(_daily_row("hrv_nightly", v, "ms", local_date, s))
    return rows


# --- auth + extract ----------------------------------------------------------

def _access_token(http: httpx.Client, api_key: str) -> str:
    """Exchange the API key for a bearer token. The live endpoint answers with
    the bare JWT as text/plain (not the JSON post_json expects); a JSON body
    carrying ``access_token`` is accepted too in case that changes."""
    r = http.post(TOKEN_URL, headers={"Authorization": f"ApiKey {api_key}"})
    r.raise_for_status()
    body = r.text.strip()
    if body.startswith("{"):
        data = r.json()
        body = (data.get("access_token") or data.get("accessToken") or "").strip()
    if not body:
        raise ValueError("empty token response")
    return body


def extract(
    http: httpx.Client,
    conn: Connection,
    app: AppConfig,
    since: date,
    until: date,
    *,
    dry_run: bool = False,
) -> SourceResult:
    result = SourceResult(source=SOURCE)
    user_id = app.file.user_id
    if not app.secrets.garmin_api_key:
        result.error("garmin: missing GARMIN_API_KEY")
        return result

    try:
        token = _access_token(http, app.secrets.garmin_api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            result.error("garmin: API key rejected or expired — generate a new one at "
                         "connect.garmin.com/app/settings/personalApiToken")
        else:
            result.error(f"garmin token exchange: {e!r}")
        return result
    except Exception as e:  # noqa: BLE001
        result.error(f"garmin token exchange: {e!r}")
        return result
    headers = {"Authorization": f"Bearer {token}"}

    daily: list[dict] = []
    sessions: list[dict] = []

    for a, b in _chunks(since, until, _SUMMARY_RANGE_DAYS):
        try:
            daily += _summary_rows(get_json(
                http, f"{BASE}/usersummary-service/v2/dailysummaries",
                params={"startDate": a.isoformat(), "endDate": b.isoformat()},
                headers=headers))
        except Exception as e:  # noqa: BLE001
            result.error(f"garmin daily summaries {a}..{b}: {e!r}")
        try:
            daily += _hrv_rows(get_json(
                http, f"{BASE}/hrv-service/hrv/daily/{a.isoformat()}/{b.isoformat()}",
                headers=headers))
        except Exception as e:  # noqa: BLE001
            result.error(f"garmin hrv {a}..{b}: {e!r}")

    for a, b in _chunks(since, until, _SLEEP_RANGE_DAYS):
        try:
            nights = get_json(
                http, f"{BASE}/sleep-service/sleep/dailySleepSummariesByDate",
                params={"startDate": a.isoformat(), "endDate": b.isoformat()},
                headers=headers)
        except Exception as e:  # noqa: BLE001
            result.error(f"garmin sleep {a}..{b}: {e!r}")
            continue
        for night in nights or []:
            session, spo2 = _sleep_rows(night)
            if session is not None:
                sessions.append(session)
            daily += spo2

    d = since
    while d <= until:
        try:
            daily += _resting_hr_rows(get_json(
                http, f"{BASE}/wellness-service/wellness/dailyHeartRate",
                params={"date": d.isoformat()}, headers=headers), d)
        except Exception as e:  # noqa: BLE001
            result.error(f"garmin daily heart rate {d}: {e!r}")
        d += timedelta(days=1)

    if dry_run:
        logger.info("garmin: would upsert %d daily rows, %d sleep sessions",
                    len(daily), len(sessions))
        return result

    try:
        result.add("raw.daily_metrics", upsert_daily_metrics(conn, user_id, daily))
    except Exception as e:  # noqa: BLE001
        result.error(f"garmin daily upsert: {e!r}")
    for s in sessions:
        try:
            upsert_sleep(conn, user_id, s, [])
            result.add("raw.sleep_sessions", 1)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            result.error(f"garmin sleep upsert {s['session_uid']}: {e!r}")
    return result
