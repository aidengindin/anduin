"""Google Health API (v4) extractor for the Fitbit Air.

Targets ``health.googleapis.com/v4`` — the successor to the Fitbit Web API,
which Google is sunsetting (~Sept 2026). Auth is Google OAuth2 (refresh token
persisted via anduin.state) bearing the ``googlehealth.*`` scopes.

One consistent resource replaces the old per-metric Fitbit paths::

    GET /v4/users/me/dataTypes/{dataType}/dataPoints?filter=<AIP-160>&pageSize=&pageToken=

Data types pulled here (all confirmed against the v4 catalog):

  - sleep                         : session — interval, stages, summary
  - oxygen-saturation             : sample  — fine-grained SpO2
  - heart-rate-variability        : sample  — fine-grained HRV (RMSSD)
  - daily-oxygen-saturation       : daily   — avg/min/max
  - daily-heart-rate-variability  : daily   — RMSSD
  - daily-resting-heart-rate      : daily   — bpm
  - daily-respiratory-rate        : daily   — breaths/min

Sleep lands in raw.sleep_sessions/raw.sleep_stages; fine-grained SpO2/HRV in
raw.samples; the daily summaries in raw.daily_metrics (keyed on the source's
local date). Fitbit's proprietary sleep *score* is not exposed by the API and
is intentionally not fabricated.

Also pulled: intraday heart_rate (sample) and steps/distance/active-energy-burned
(interval) into raw.samples, keeping the pre-v4 metric names + units so the
existing canonical views keep working. Their proto field names are confirmed but
the JSON envelope is INFERRED (see the emitter block); confirm against a live
response.

The exact AIP-160 filter field per record kind (interval vs sample vs daily) is
built in ``_filter_expr`` from the documented generic form and should be
sanity-checked against a real response on first run.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import get_json
from anduin.oauth import GOOGLE_HEALTH, access_token
from anduin.sources.base import SourceResult
from anduin.upsert import upsert_daily_metrics, upsert_samples, upsert_sleep

logger = logging.getLogger(__name__)

BASE = "https://health.googleapis.com/v4"
DEVICE = "fitbit_air"


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


# --- small coercion / parsing helpers -------------------------------------

def _parse_ts(s: str) -> datetime:
    """RFC-3339 timestamp -> aware UTC datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tz_offset_minutes(s: str) -> int | None:
    """Local UTC offset (minutes) embedded in an RFC-3339 timestamp, or None.

    A v4 sleep interval time may carry the wearer's local offset (e.g.
    ...T23:10:00-04:00), which sleep-regularity (SRI) needs to align clock time
    across nights. A trailing 'Z' or a naive string means the local offset is
    unknown -> None, and the derived views fall back to UTC (documented there)."""
    if s.endswith(("Z", "z")):
        return None
    off = datetime.fromisoformat(s).utcoffset()
    return None if off is None else int(off.total_seconds() // 60)


def _utc_offset_seconds_field(s: str | None) -> int | None:
    """Parse a v4 ``*UtcOffset`` field ("-14400s") into whole minutes, or None.

    Fitbit-via-Google sleep intervals keep ``startTime`` as a 'Z' UTC stamp and
    carry the wearer's local offset in a sibling ``startUtcOffset`` field encoded
    as a signed seconds string with a trailing 's'."""
    if not s:
        return None
    s = s.strip().rstrip("sS")
    try:
        return int(s) // 60
    except ValueError:
        return None


def _coerce_int(v) -> int | None:
    # int64 proto fields arrive as JSON strings ("420"); tolerate None/blank.
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _local_date(d: dict) -> date:
    """Structured {year, month, day} -> date (already the source's local date)."""
    return date(int(d["year"]), int(d["month"]), int(d["day"]))


def _daily_row(metric: str, value: float, unit: str, local_date: date, dp: dict) -> dict:
    return {
        "source": "google_health",
        "device": DEVICE,
        "recording_method": "device",
        "metric": metric,
        "value": value,
        "unit": unit,
        "local_date": local_date,
        "tz_offset_minutes": None,
        "natural_key": f"{metric}|{local_date.isoformat()}",
        "raw": dp,
    }


def _sample_row(metric: str, value: float, unit: str, ts: datetime, dp: dict) -> dict:
    return {
        "source": "google_health",
        "device": DEVICE,
        "recording_method": "wrist_optical",
        "metric": metric,
        "value": value,
        "unit": unit,
        "valid_from": ts,
        "valid_to": ts,
        "natural_key": f"{metric}|{ts.isoformat()}",
        "raw": dp,
    }


def _interval_row(metric: str, value: float, unit: str,
                  start: datetime, end: datetime, dp: dict) -> dict:
    """A raw.samples row spanning an interval (steps/distance/energy buckets)."""
    return {
        "source": "google_health",
        "device": DEVICE,
        "recording_method": "device",
        "metric": metric,
        "value": value,
        "unit": unit,
        "valid_from": start,
        "valid_to": end,
        "natural_key": f"{metric}|{start.isoformat()}",
        "raw": dp,
    }


# --- emitters (pure: v4 dataPoint dict -> row dict(s)) ----------------------

def _emit_sleep(dp: dict) -> tuple[dict, list[dict]]:
    s = dp.get("sleep", {})
    interval = s.get("interval", {})
    started_at = _parse_ts(interval["startTime"])
    ended_at = _parse_ts(interval["endTime"])
    session_uid = dp.get("name") or f"sleep|{started_at.isoformat()}"
    summary = s.get("summary") or {}

    # Prefer the explicit ``startUtcOffset`` field (real Fitbit payloads); fall
    # back to an offset embedded in the startTime string (older/other sources).
    tz_off = _utc_offset_seconds_field(interval.get("startUtcOffset"))
    if tz_off is None:
        tz_off = _tz_offset_minutes(interval["startTime"])

    session = {
        "source": "google_health",
        "session_uid": session_uid,
        "device": DEVICE,
        "recording_method": "device",
        "started_at": started_at,
        "ended_at": ended_at,
        "tz_offset_minutes": tz_off,
        # v4 does not always flag a main sleep; tolerate absence.
        "is_main_sleep": s.get("isMainSleep"),
        "sleep_type": s.get("type"),
        "minutes_asleep": _coerce_int(summary.get("minutesAsleep")),
        "minutes_awake": _coerce_int(summary.get("minutesAwake")),
        "minutes_in_sleep_period": _coerce_int(summary.get("minutesInSleepPeriod")),
        "minutes_to_fall_asleep": _coerce_int(summary.get("minutesToFallAsleep")),
        "minutes_after_wakeup": _coerce_int(summary.get("minutesAfterWakeUp")),
        "efficiency": _coerce_float(summary.get("efficiency")),
        "summary": summary or None,
        "raw": dp,
        "natural_key": f"sleep|{session_uid}",
    }

    stages = [
        {
            "source": "google_health",
            "session_uid": session_uid,
            "stage": seg.get("type"),
            "started_at": _parse_ts(seg["startTime"]),
            "ended_at": _parse_ts(seg["endTime"]),
        }
        for seg in s.get("stages", [])
    ]
    return session, stages


def _emit_spo2_sample(dp: dict) -> dict | None:
    o = dp.get("oxygenSaturation", {})
    ts = o.get("sampleTime", {}).get("physicalTime")
    value = _coerce_float(o.get("percentage"))
    if ts is None or value is None:
        return None
    return _sample_row("spo2", value, "%", _parse_ts(ts), dp)


def _emit_hrv_sample(dp: dict) -> dict | None:
    h = dp.get("heartRateVariability", {})
    ts = h.get("sampleTime", {}).get("physicalTime")
    value = _coerce_float(h.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds"))
    if ts is None or value is None:
        return None
    return _sample_row("hrv", value, "ms", _parse_ts(ts), dp)


def _emit_resting_heart_rate(dp: dict) -> dict | None:
    r = dp.get("dailyRestingHeartRate", {})
    value = _coerce_float(r.get("beatsPerMinute"))
    if "date" not in r or value is None:
        return None
    return _daily_row("resting_heart_rate", value, "bpm", _local_date(r["date"]), dp)


def _emit_respiratory_rate(dp: dict) -> dict | None:
    r = dp.get("dailyRespiratoryRate", {})
    value = _coerce_float(r.get("breathsPerMinute"))
    if "date" not in r or value is None:
        return None
    return _daily_row("respiratory_rate", value, "br/min", _local_date(r["date"]), dp)


def _emit_hrv_daily(dp: dict) -> dict | None:
    h = dp.get("dailyHeartRateVariability", {})
    value = _coerce_float(h.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds"))
    if "date" not in h or value is None:
        return None
    return _daily_row("hrv_daily_rmssd", value, "ms", _local_date(h["date"]), dp)


def _emit_spo2_daily(dp: dict) -> list[dict]:
    o = dp.get("dailyOxygenSaturation", {})
    if "date" not in o:
        return []
    ld = _local_date(o["date"])
    out = []
    for suffix, field in (("avg", "average"), ("min", "minimum"), ("max", "maximum")):
        value = _coerce_float(o.get(field))
        if value is None:
            continue
        out.append(_daily_row(f"spo2_daily_{suffix}", value, "%", ld, dp))
    return out


# Intraday heart_rate/steps/distance/active_energy. The proto field names below
# are confirmed (beats_per_minute / count / meters / kcal); the JSON envelope
# (camelCase nesting key + interval/sampleTime shape) is INFERRED from the sleep
# and spo2/hrv payloads and not yet seen live. Metric names + units are held to
# the pre-v4 values on purpose: canonical.neat_energy / total_energy key on
# metric='active_energy', and distance stays in km. Confirm against a real
# response; if a key differs it is a one-line change (raw is always stored).

def _emit_heart_rate(dp: dict) -> dict | None:
    h = dp.get("heartRate", {})
    ts = h.get("sampleTime", {}).get("physicalTime")
    value = _coerce_float(h.get("beatsPerMinute"))
    if ts is None or value is None:
        return None
    return _sample_row("heart_rate", value, "bpm", _parse_ts(ts), dp)


def _emit_steps(dp: dict) -> dict | None:
    s = dp.get("steps", {})
    interval = s.get("interval", {})
    value = _coerce_float(s.get("count"))
    if "startTime" not in interval or value is None:
        return None
    return _interval_row(
        "steps", value, "count",
        _parse_ts(interval["startTime"]), _parse_ts(interval["endTime"]), dp,
    )


def _emit_distance(dp: dict) -> dict | None:
    d = dp.get("distance", {})
    interval = d.get("interval", {})
    meters = _coerce_float(d.get("meters"))
    if "startTime" not in interval or meters is None:
        return None
    return _interval_row(
        "distance", meters / 1000.0, "km",  # v4 is meters; stored as km
        _parse_ts(interval["startTime"]), _parse_ts(interval["endTime"]), dp,
    )


def _emit_active_energy(dp: dict) -> dict | None:
    e = dp.get("activeEnergyBurned", {})
    interval = e.get("interval", {})
    value = _coerce_float(e.get("kcal"))
    if "startTime" not in interval or value is None:
        return None
    return _interval_row(
        "active_energy", value, "kcal",
        _parse_ts(interval["startTime"]), _parse_ts(interval["endTime"]), dp,
    )


# --- v4 list client --------------------------------------------------------

# (dataType endpoint id, filter id, record kind, page_size)
# record kind selects the AIP-160 time field the range filter is applied to.
_SAMPLE_TYPES = [
    ("oxygen-saturation", "oxygen_saturation", "sample", 10000),
    ("heart-rate-variability", "heart_rate_variability", "sample", 10000),
    ("heart-rate", "heart_rate", "sample", 10000),
]
# Interval types -> raw.samples spanning valid_from..valid_to.
_INTERVAL_TYPES = [
    ("steps", "steps", "interval", 10000),
    ("distance", "distance", "interval", 10000),
    ("active-energy-burned", "active_energy_burned", "interval", 10000),
]
_DAILY_TYPES = [
    ("daily-oxygen-saturation", "daily_oxygen_saturation", "daily", 1440),
    ("daily-heart-rate-variability", "daily_heart_rate_variability", "daily", 1440),
    ("daily-resting-heart-rate", "daily_resting_heart_rate", "daily", 1440),
    ("daily-respiratory-rate", "daily_respiratory_rate", "daily", 1440),
]
# Sleep is a session type; pageSize caps at 25, so it always paginates.
_SLEEP_TYPE = ("sleep", "sleep", "session", 25)


def _filter_expr(filter_id: str, kind: str, since: date, until: date) -> str:
    """Build the AIP-160 range filter for a data type (verified live against v4).

    The member and value format both vary by record kind:
      - session (sleep): interval.END_time, RFC-3339 — sleep's "special handling"
      - interval:        interval.start_time, RFC-3339
      - sample:          sample_time.physical_time, RFC-3339
      - daily:           date, CIVIL date (YYYY-MM-DD, no time component)
    ``until`` is inclusive, so the exclusive upper bound is until + 1 day.
    """
    end_date = until + timedelta(days=1)
    if kind == "daily":
        field = "date"
        start, end = since.isoformat(), end_date.isoformat()
    else:
        field = {
            "session": "interval.end_time",
            "interval": "interval.start_time",
            "sample": "sample_time.physical_time",
        }[kind]
        start = f"{since.isoformat()}T00:00:00Z"
        end = f"{end_date.isoformat()}T00:00:00Z"
    lhs = f"{filter_id}.{field}"
    return f'{lhs} >= "{start}" AND {lhs} < "{end}"'


def _list_datapoints(
    http: httpx.Client,
    token: str,
    data_type: str,
    filter_expr: str,
    page_size: int,
) -> Iterator[dict]:
    """Yield every dataPoint for a type across the range, following pageTokens."""
    url = f"{BASE}/users/me/dataTypes/{data_type}/dataPoints"
    page_token: str | None = None
    while True:
        params = {"filter": filter_expr, "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        payload = get_json(http, url, params=params, headers=_bearer(token))
        if not isinstance(payload, dict):
            return
        for dp in payload.get("dataPoints", []) or []:
            yield dp
        page_token = payload.get("nextPageToken")
        if not page_token:
            return


# --- orchestration ---------------------------------------------------------

def extract(
    http: httpx.Client,
    conn: Connection,
    app: AppConfig,
    since: date,
    until: date,
    *,
    dry_run: bool = False,
) -> SourceResult:
    result = SourceResult(source="google_health")
    user_id = app.file.user_id
    if not app.secrets.google_health_client_id or not app.secrets.google_health_client_secret:
        result.error("google_health: missing GOOGLE_HEALTH_CLIENT_ID/SECRET")
        return result

    try:
        token = access_token(
            http,
            GOOGLE_HEALTH,
            app.file.state_dir,
            app.secrets.google_health_client_id,
            app.secrets.google_health_client_secret,
        )
    except Exception as e:  # noqa: BLE001
        result.error(f"google_health auth: {e!r}")
        return result

    dry_run_total = 0

    # Sleep (sessions + stages).
    data_type, filter_id, kind, page_size = _SLEEP_TYPE
    try:
        sessions = 0
        for dp in _list_datapoints(
            http, token, data_type,
            _filter_expr(filter_id, kind, since, until), page_size,
        ):
            session, stages = _emit_sleep(dp)
            sessions += 1
            if dry_run:
                dry_run_total += 1 + len(stages)
                continue
            upsert_sleep(conn, user_id, session, stages)
        if not dry_run and sessions:
            result.add("raw.sleep_sessions", sessions)
        logger.info("google_health %s: OK, %d session(s)", data_type, sessions)
    except Exception as e:  # noqa: BLE001
        result.error(f"sleep: {e!r}")
        logger.warning("google_health %s: FAILED %r", data_type, e)

    # Sample + interval types both land in raw.samples via a single-row emitter.
    row_emitters = {
        "oxygen-saturation": _emit_spo2_sample,
        "heart-rate-variability": _emit_hrv_sample,
        "heart-rate": _emit_heart_rate,
        "steps": _emit_steps,
        "distance": _emit_distance,
        "active-energy-burned": _emit_active_energy,
    }
    for data_type, filter_id, kind, page_size in _SAMPLE_TYPES + _INTERVAL_TYPES:
        try:
            rows = []
            for dp in _list_datapoints(
                http, token, data_type,
                _filter_expr(filter_id, kind, since, until), page_size,
            ):
                row = row_emitters[data_type](dp)
                if row is not None:
                    rows.append(row)
            logger.info("google_health %s: OK, %d row(s)", data_type, len(rows))
            if dry_run:
                dry_run_total += len(rows)
                continue
            n = upsert_samples(conn, user_id, rows)
            result.add("raw.samples", n)
        except Exception as e:  # noqa: BLE001
            result.error(f"{data_type} {since}..{until}: {e!r}")
            logger.warning("google_health %s: FAILED %r", data_type, e)

    # Daily summaries -> raw.daily_metrics.
    daily_emitters = {
        "daily-oxygen-saturation": _emit_spo2_daily,
        "daily-heart-rate-variability": lambda dp: [r for r in [_emit_hrv_daily(dp)] if r],
        "daily-resting-heart-rate": lambda dp: [r for r in [_emit_resting_heart_rate(dp)] if r],
        "daily-respiratory-rate": lambda dp: [r for r in [_emit_respiratory_rate(dp)] if r],
    }
    for data_type, filter_id, kind, page_size in _DAILY_TYPES:
        try:
            rows = []
            for dp in _list_datapoints(
                http, token, data_type,
                _filter_expr(filter_id, kind, since, until), page_size,
            ):
                rows.extend(daily_emitters[data_type](dp))
            logger.info("google_health %s: OK, %d row(s)", data_type, len(rows))
            if dry_run:
                dry_run_total += len(rows)
                continue
            n = upsert_daily_metrics(conn, user_id, rows)
            result.add("raw.daily_metrics", n)
        except Exception as e:  # noqa: BLE001
            result.error(f"{data_type} {since}..{until}: {e!r}")
            logger.warning("google_health %s: FAILED %r", data_type, e)

    if dry_run:
        result.add("(dry-run rows)", dry_run_total)
    return result
