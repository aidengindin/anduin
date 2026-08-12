"""intervals.icu extractor.

Pulls the activity list for the window, then per-activity streams. Auth shape
matches the headache-tracker pattern (HTTP Basic with literal username
'API_KEY').
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import get_json
from anduin.sources.base import SourceResult
from anduin.upsert import (
    upsert_activity,
    upsert_activity_streams,
    upsert_daily_metrics,
)

logger = logging.getLogger(__name__)

BASE = "https://intervals.icu/api/v1/athlete"
# Streams are NOT athlete-scoped: /api/v1/activity/{id}/streams (verified live).
API_ROOT = "https://intervals.icu/api/v1"

STREAM_METRICS = (
    "heartrate",
    "watts",
    "cadence",
    "distance",
    "altitude",
    "temp",
    "velocity_smooth",  # intervals.icu's name for speed; "speed" 422s (verified live)
)


def _auth(api_key: str) -> tuple[str, str]:
    return ("API_KEY", api_key)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _activity_window(act: dict) -> tuple[datetime, datetime]:
    """Choose the activity start/end instants, preferring true-UTC fields.

    intervals.icu exposes both ``start_date``/``end_date`` (carrying a 'Z' or
    explicit offset) and ``start_date_local``/``end_date_local`` (naive
    wall-clock, no offset). Since ``_parse_dt`` labels any naive datetime as
    UTC, using the *_local fields would mislabel local time as UTC and shift
    the activity (and every per-second stream row) by the athlete's offset.
    Prefer the UTC fields; fall back to *_local only when UTC is absent.
    """
    start_str = act.get("start_date") or act.get("start_date_local")
    started_at = _parse_dt(start_str)

    end_str = act.get("end_date") or act.get("end_date_local")
    if end_str:
        ended_at = _parse_dt(end_str)
    else:
        duration = float(act.get("elapsed_time") or act.get("moving_time") or 0)
        ended_at = started_at + timedelta(seconds=duration)
    return started_at, ended_at


def _is_skipped(act: dict, patterns) -> bool:
    """True when the activity name matches a configured skip pattern.

    Liftosaur workouts are mirrored into intervals.icu by a separate sync
    service, so they arrive here as duplicates of what the liftosaur extractor
    already ingests. Those mirrored activities carry "Liftosaur" in the name,
    which is the only reliable marker on the intervals side.
    """
    name = (act.get("name") or "").lower()
    if not name:
        return False
    return any(p.lower() in name for p in patterns or ())


def _list_activities(
    http: httpx.Client, athlete_id: str, api_key: str, start: date, end: date
) -> list[dict]:
    url = f"{BASE}/{athlete_id}/activities"
    params = {"oldest": start.isoformat(), "newest": end.isoformat()}
    data = get_json(http, url, params=params, auth=_auth(api_key))
    return data if isinstance(data, list) else []


# PMC: intervals already computes Fitness (CTL) / Fatigue (ATL) daily; we ingest
# them rather than recompute. Form (TSB) = ctl - atl is derived downstream.
_WELLNESS_METRICS = ("ctl", "atl")


def _fetch_wellness(http: httpx.Client, athlete_id: str, api_key: str, start: date, end: date):
    url = f"{BASE}/{athlete_id}/wellness"
    params = {"oldest": start.isoformat(), "newest": end.isoformat()}
    data = get_json(http, url, params=params, auth=_auth(api_key))
    return data if isinstance(data, list) else []


def _wellness_rows(records) -> list[dict]:
    """Map wellness records into raw.daily_metrics rows for ctl/atl. Skips records
    with no date and metrics whose value is absent."""
    out: list[dict] = []
    for rec in records or []:
        d = rec.get("id")
        if not d:
            continue
        local_date = date.fromisoformat(d)
        for metric in _WELLNESS_METRICS:
            value = rec.get(metric)
            if value is None:
                continue
            out.append({
                "source": "intervals",
                "device": None,
                "recording_method": "intervals",
                "metric": metric,
                "value": float(value),
                "unit": None,
                "local_date": local_date,
                "tz_offset_minutes": None,
                "natural_key": f"{metric}|{local_date.isoformat()}",
                "raw": rec,
            })
    return out


def _fetch_streams(http: httpx.Client, api_key: str, activity_id: str):
    url = f"{API_ROOT}/activity/{activity_id}/streams"
    params = {"types": ",".join(STREAM_METRICS)}
    return get_json(http, url, params=params, auth=_auth(api_key))


def _emit_streams(activity_id: str, started_at: datetime, streams_payload) -> list[dict]:
    if isinstance(streams_payload, dict):
        streams = streams_payload.get("streams") or []
    elif isinstance(streams_payload, list):
        streams = streams_payload
    else:
        streams = []
    out: list[dict] = []
    for s in streams:
        metric = s.get("type")
        data = s.get("data")
        if not metric or not isinstance(data, list):
            continue
        for i, v in enumerate(data):
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            out.append({
                "source": "intervals",
                "activity_uid": activity_id,
                "t": started_at + timedelta(seconds=i),
                "metric": metric,
                "value": fv,
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
    result = SourceResult(source="intervals")
    user_id = app.file.user_id
    if not app.secrets.intervals_api_key or not app.secrets.intervals_athlete_id:
        result.error("intervals: missing INTERVALS_API_KEY or INTERVALS_ATHLETE_ID")
        return result

    athlete = app.secrets.intervals_athlete_id
    key = app.secrets.intervals_api_key

    try:
        activities = _list_activities(http, athlete, key, since, until)
    except Exception as e:  # noqa: BLE001
        result.error(f"list activities {since}..{until}: {e!r}")
        return result

    skip_patterns = app.file.intervals.skip_name_contains
    skipped = [a for a in activities if _is_skipped(a, skip_patterns)]
    if skipped:
        activities = [a for a in activities if not _is_skipped(a, skip_patterns)]
        logger.info(
            "intervals: skipping %d mirrored activities (%s)",
            len(skipped),
            ", ".join(sorted({str(a.get("name")) for a in skipped})),
        )

    logger.info("intervals: %d activities in %s..%s", len(activities), since, until)

    for act in activities:
        aid = str(act.get("id") or act.get("activity_id") or "")
        if not aid:
            continue
        if not (act.get("start_date") or act.get("start_date_local")):
            continue
        started_at, ended_at = _activity_window(act)

        row = {
            "source": "intervals",
            "activity_uid": aid,
            "device": act.get("device_name"),
            "recording_method": act.get("source") or act.get("file_type"),
            "sport": act.get("type") or act.get("sport"),
            "started_at": started_at,
            "ended_at": ended_at,
            "summary": act,
            "raw": act,
            "natural_key": aid,
        }
        if dry_run:
            logger.info("would upsert activity %s (%s)", aid, started_at)
        else:
            try:
                upsert_activity(conn, user_id, row)
                result.add("raw.activities", 1)
            except Exception as e:  # noqa: BLE001
                result.error(f"activity {aid}: {e!r}")
                continue

        if not app.file.intervals.pull_streams:
            continue
        try:
            streams_payload = _fetch_streams(http, key, aid)
        except Exception as e:  # noqa: BLE001
            result.error(f"streams {aid}: {e!r}")
            continue
        rows = _emit_streams(aid, started_at, streams_payload)
        if dry_run:
            logger.info("  would upsert %d stream rows", len(rows))
            continue
        try:
            n = upsert_activity_streams(conn, user_id, rows)
            result.add("raw.activity_streams", n)
        except Exception as e:  # noqa: BLE001
            result.error(f"stream upsert {aid}: {e!r}")

    # PMC (fitness/fatigue) from the wellness feed -> raw.daily_metrics.
    try:
        wellness = _wellness_rows(_fetch_wellness(http, athlete, key, since, until))
    except Exception as e:  # noqa: BLE001
        result.error(f"wellness {since}..{until}: {e!r}")
        wellness = []
    if dry_run:
        logger.info("would upsert %d wellness (ctl/atl) rows", len(wellness))
    elif wellness:
        try:
            n = upsert_daily_metrics(conn, user_id, wellness)
            result.add("raw.daily_metrics", n)
        except Exception as e:  # noqa: BLE001
            result.error(f"wellness upsert: {e!r}")

    return result
