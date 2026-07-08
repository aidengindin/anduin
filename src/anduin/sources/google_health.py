"""Google Health API extractor for the Fitbit Air.

Uses Google's OAuth2 (refresh token persisted via anduin.state) and the
Fitbit-compatible endpoints exposed under api.fitbit.com — the Google Health
migration delegates these endpoints under the same paths, accepted with a
Google access token bearing the fitness.* scopes.

Per-metric `list` calls:
  - intraday heart_rate     : /1/user/-/activities/heart/date/{d}/1d/1sec.json
  - intraday steps          : /1/user/-/activities/steps/date/{d}/1d/1min.json
  - intraday distance       : /1/user/-/activities/distance/date/{d}/1d/1min.json
  - intraday active_energy  : /1/user/-/activities/calories/date/{d}/1d/1min.json
  - sleep (incl. SpO2/HRV/skin_temp) : /1.2/user/-/sleep/date/{d}.json + companion
                                      /1/user/-/spo2/date/{d}.json (etc.)

Endpoints subject to verification against current Google Health API docs —
the structural code (auth, looping, upsert, idempotency) does not change if a
URL needs to be swapped.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import get_json
from anduin.oauth import GOOGLE_HEALTH, access_token
from anduin.sources.base import SourceResult
from anduin.upsert import upsert_samples

logger = logging.getLogger(__name__)

BASE = "https://api.fitbit.com"

INTRADAY = [
    # (metric, path_template, detail_level)
    ("heart_rate",    "/1/user/-/activities/heart/date/{d}/1d/1sec.json",   "1sec"),
    ("steps",         "/1/user/-/activities/steps/date/{d}/1d/1min.json",   "1min"),
    ("distance",      "/1/user/-/activities/distance/date/{d}/1d/1min.json","1min"),
    ("active_energy", "/1/user/-/activities/calories/date/{d}/1d/1min.json","1min"),
]


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def _date_range(since: date, until: date):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def _intraday_unit(metric: str) -> str:
    return {
        "heart_rate": "bpm",
        "steps": "count",
        "distance": "km",
        "active_energy": "kcal",
    }[metric]


def _fetch_intraday(http: httpx.Client, token: str, path_tmpl: str, d: date):
    url = BASE + path_tmpl.format(d=d.isoformat())
    return get_json(http, url, headers=_bearer(token))


def _emit_intraday(metric: str, detail: str, d: date, payload) -> list[dict]:
    """Walk the Fitbit intraday response: top-level day total + intraday dataset."""
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    # Select the day-total base key deterministically: it starts with
    # 'activities-' but must NOT itself be the '-intraday' key. Otherwise, if
    # the '-intraday' key iterates first, we'd derive a doubled
    # '...-intraday-intraday' key (absent) and silently drop the dataset.
    key = next(
        (
            k
            for k in payload
            if k.startswith("activities-") and not k.endswith("-intraday")
        ),
        None,
    )
    intraday_key = f"{key}-intraday" if key else None
    intraday = payload.get(intraday_key, {}) if intraday_key else {}
    dataset = intraday.get("dataset", []) or []
    unit = _intraday_unit(metric)

    if detail == "1sec":
        bucket = timedelta(seconds=1)
    else:
        bucket = timedelta(minutes=1)

    day_midnight = datetime.combine(d, time.min, tzinfo=timezone.utc)
    for entry in dataset:
        t_str = entry.get("time")
        v = entry.get("value")
        if t_str is None or v is None:
            continue
        hh, mm, ss = (int(x) for x in t_str.split(":"))
        valid_from = day_midnight + timedelta(hours=hh, minutes=mm, seconds=ss)
        valid_to = valid_from + bucket
        try:
            value = float(v)
        except (TypeError, ValueError):
            continue
        out.append({
            "source": "google_health",
            "device": "fitbit_air",
            "recording_method": "wrist_optical" if metric == "heart_rate" else "device",
            "metric": metric,
            "value": value,
            "unit": unit,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "natural_key": f"{metric}|{valid_from.isoformat()}",
            "raw": entry,
        })
    return out


def _fetch_companion(http: httpx.Client, token: str, kind: str, d: date):
    """spo2 / hrv / temp-skin daily endpoints."""
    path = {
        "sleep_spo2":      f"/1/user/-/spo2/date/{d.isoformat()}/all.json",
        "sleep_hrv":       f"/1/user/-/hrv/date/{d.isoformat()}/all.json",
        "sleep_skin_temp": f"/1/user/-/temp/skin/date/{d.isoformat()}.json",
    }[kind]
    return get_json(http, BASE + path, headers=_bearer(token))


def _emit_companion(kind: str, d: date, payload) -> list[dict]:
    """Spread companion payloads across whatever timestamps they carry."""
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    # Try common shapes.
    items = (
        payload.get("spo2")
        or payload.get("hrv")
        or payload.get("tempSkin")
        or []
    )
    if isinstance(items, dict):
        items = [items]
    day_midnight = datetime.combine(d, time.min, tzinfo=timezone.utc)
    for idx, it in enumerate(items):
        # Try several timestamp shapes. Track whether the item carried a usable
        # per-reading timestamp; if not, we fall back to date-only for
        # valid_from but must still make the natural_key unique per item so
        # distinct readings on the same day don't overwrite each other.
        raw_ts = it.get("dateTime") or it.get("minute")
        ts = raw_ts or d.isoformat()
        has_ts = raw_ts is not None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = day_midnight
            has_ts = False
        # Best-effort per-reading identity: prefer the real timestamp, else
        # disambiguate by the item's index within the day.
        natural_key = (
            f"{kind}|{dt.isoformat()}"
            if has_ts
            else f"{kind}|{d.isoformat()}|{idx}"
        )
        # Find a numeric "value".
        val = it.get("value")
        if isinstance(val, dict):
            val = val.get("avg") or val.get("nightlyTemperature") or val.get("dailyRmssd")
        if val is None:
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        out.append({
            "source": "google_health",
            "device": "fitbit_air",
            "recording_method": "wrist_optical",
            "metric": kind,
            "value": fv,
            "unit": {"sleep_spo2": "%", "sleep_hrv": "ms", "sleep_skin_temp": "C"}[kind],
            "valid_from": dt,
            "valid_to": dt,
            "natural_key": natural_key,
            "raw": it,
        })
    return out


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

    for d in _date_range(since, until):
        # Buffer per-day so a single failure never discards more than one day
        # of already-fetched data, and so partial progress lands durably.
        day_rows: list[dict] = []

        for metric, path_tmpl, detail in INTRADAY:
            try:
                payload = _fetch_intraday(http, token, path_tmpl, d)
            except Exception as e:  # noqa: BLE001
                result.error(f"{metric} {d}: {e!r}")
                continue
            day_rows.extend(_emit_intraday(metric, detail, d, payload))

        for kind in ("sleep_spo2", "sleep_hrv", "sleep_skin_temp"):
            try:
                payload = _fetch_companion(http, token, kind, d)
            except Exception as e:  # noqa: BLE001
                result.error(f"{kind} {d}: {e!r}")
                continue
            day_rows.extend(_emit_companion(kind, d, payload))

        if dry_run:
            dry_run_total += len(day_rows)
            continue

        # Upsert incrementally so progress is durable: one failing day is
        # recorded to result.errors and does not abort the whole run.
        try:
            n = upsert_samples(conn, day_rows)
            result.add("raw.samples", n)
        except Exception as e:  # noqa: BLE001
            result.error(f"google_health upsert {d}: {e!r}")

    if dry_run:
        logger.info("google_health: would upsert %d sample rows", dry_run_total)

    return result
