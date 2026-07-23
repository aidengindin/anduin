"""Liftosaur extractor — structured strength (workout -> exercise -> set).

Liftosaur's API (`GET /api/v1/history`, Bearer auth) returns workout records as
Liftohistory *text*, not JSON:

    {"data": {"records": [{"id": 1779877039893, "text": "2026-05-27 ... / exercises: {...}"}],
              "hasMore": false, "nextCursor": 123}}

We page through with cursor/limit, filter to the window client-side, and parse
each record with anduin.sources.liftohistory (unit-tested). The workout header
lands in the unified raw.activities table (sport = 'strength'); exercises/sets
go to the strength child tables.

Natural keys:
  activity : record id
  exercise : f"{activity}/{exercise_index}"
  set      : (activity, exercise, set_index)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import get_json
from anduin.sources.base import SourceResult
from anduin.sources.liftohistory import build_strength_rows
from anduin.upsert import upsert_strength

logger = logging.getLogger(__name__)

HISTORY_URL = "https://www.liftosaur.com/api/v1/history"
PAGE_LIMIT = 200


def _fetch_page(
    http: httpx.Client, api_key: str, since: date, until: date, cursor: str | None
) -> dict:
    params: dict[str, str] = {
        "startDate": since.isoformat(),
        "endDate": until.isoformat(),
        "limit": str(PAGE_LIMIT),
    }
    if cursor:
        params["cursor"] = cursor
    resp = get_json(
        http,
        HISTORY_URL,
        params=params,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return resp.get("data") or {} if isinstance(resp, dict) else {}


def extract(
    http: httpx.Client,
    conn: Connection,
    app: AppConfig,
    since: date,
    until: date,
    *,
    dry_run: bool = False,
) -> SourceResult:
    result = SourceResult(source="liftosaur")
    user_id = app.file.user_id
    if not app.secrets.liftosaur_api_key:
        result.error("liftosaur: missing LIFTOSAUR_API_KEY")
        return result

    key = app.secrets.liftosaur_api_key
    start_ts = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    n_workouts = n_exercises = n_sets = 0
    cursor: str | None = None
    while True:
        try:
            data = _fetch_page(http, key, since, until, cursor)
        except Exception as e:  # noqa: BLE001
            result.error(f"liftosaur fetch: {e!r}")
            return result

        for rec in data.get("records") or []:
            rid, text = rec.get("id"), rec.get("text")
            if rid is None or not text:
                continue
            rows = build_strength_rows(rid, text)
            activity = rows["activity"]
            started_at = activity["started_at"]
            # Guard the window client-side; the API date filter is inclusive-ish.
            if started_at is None or not (start_ts <= started_at < end_ts):
                continue

            if dry_run:
                logger.info(
                    "would upsert strength activity %s with %d exercises, %d sets",
                    activity["activity_uid"],
                    len(rows["exercises"]),
                    len(rows["sets"]),
                )
                continue
            try:
                upsert_strength(conn, user_id, activity, rows["exercises"], rows["sets"])
                n_workouts += 1
                n_exercises += len(rows["exercises"])
                n_sets += len(rows["sets"])
            except Exception as e:  # noqa: BLE001
                result.error(f"activity {activity['activity_uid']}: {e!r}")

        next_cursor = data.get("nextCursor")
        if not data.get("hasMore") or next_cursor is None:
            break
        cursor = str(next_cursor)

    result.add("raw.activities", n_workouts)
    result.add("raw.strength_exercises", n_exercises)
    result.add("raw.strength_sets", n_sets)
    return result
