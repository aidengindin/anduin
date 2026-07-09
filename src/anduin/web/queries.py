"""Read-only queries over the reconciled ``canonical.*`` layer.

Every function takes a psycopg connection (dict row factory) and returns plain
Python data ready for templating or JSON. Nothing here writes. The canonical
views already resolve source precedence (see migration 0007), so charts and
rollups read them directly rather than re-implementing reconciliation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from psycopg import Connection

# --- metric registry -------------------------------------------------------
# User-supplied metric names arrive via the URL, so map them through a fixed
# whitelist to a canonical per-metric view. Never interpolate the raw name into
# SQL. `unit` and `label` drive the chart axis/title.
METRICS: dict[str, dict[str, str]] = {
    "heart_rate": {"view": "canonical.heart_rate", "label": "Heart rate", "unit": "bpm"},
    "steps": {"view": "canonical.steps", "label": "Steps", "unit": "count"},
    "body_weight": {"view": "canonical.body_weight", "label": "Body weight", "unit": "kg"},
    "hrv": {"view": "canonical.hrv", "label": "HRV", "unit": "ms"},
    "spo2": {"view": "canonical.spo2", "label": "SpO₂", "unit": "%"},
    "skin_temp": {"view": "canonical.skin_temp", "label": "Skin temp", "unit": "°C"},
}


def _as_utc(d: date | datetime) -> datetime:
    """Normalize a date/datetime to a tz-aware UTC datetime.

    Keeps ``timestamptz`` comparisons deterministic regardless of the server's
    session timezone (a bare date literal would be cast using that session tz).
    """
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _bucket_for_range(start: datetime, end: datetime) -> str:
    """Pick a time_bucket width from the span: fine for short ranges, coarse
    for long ones, so a chart never returns an unbounded point count."""
    span_days = (end - start).total_seconds() / 86400.0
    if span_days <= 2:
        return "1 minute"
    if span_days <= 60:
        return "1 hour"
    return "1 day"


def metric_series(
    conn: Connection,
    metric: str,
    start: date | datetime,
    end: date | datetime,
) -> dict[str, Any]:
    """Bucketed avg/min/max series for one metric over [start, end).

    Returns a dict with metadata plus ``points``: a list of
    ``{"t": <epoch seconds>, "avg", "min", "max"}`` ordered by time. Raises
    KeyError for an unknown metric (callers should 404)."""
    meta = METRICS[metric]  # KeyError => unknown metric
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    bucket = _bucket_for_range(start_dt, end_dt)
    sql = f"""
        SELECT time_bucket(%(bucket)s::interval, valid_from) AS t,
               avg(value) AS avg,
               min(value) AS min,
               max(value) AS max
        FROM {meta['view']}
        WHERE valid_from >= %(start)s AND valid_from < %(end)s
        GROUP BY t
        ORDER BY t
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"bucket": bucket, "start": start_dt, "end": end_dt})
        rows = cur.fetchall()
    points = [
        {
            "t": int(r["t"].timestamp()),
            "avg": float(r["avg"]) if r["avg"] is not None else None,
            "min": float(r["min"]) if r["min"] is not None else None,
            "max": float(r["max"]) if r["max"] is not None else None,
        }
        for r in rows
    ]
    return {
        "metric": metric,
        "label": meta["label"],
        "unit": meta["unit"],
        "bucket": bucket,
        "points": points,
    }


def daily_summary(
    conn: Connection,
    start: date | datetime,
    end: date | datetime,
) -> list[dict[str, Any]]:
    """Per-day rollup over [start, end): total steps, total energy (kcal), mean
    body weight, and workout count. Days with no data are omitted; merged in
    Python from four cheap grouped queries keyed by calendar day (UTC)."""
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    params = {"start": start_dt, "end": end_dt}
    by_day: dict[date, dict[str, Any]] = {}

    def _row(day: date) -> dict[str, Any]:
        return by_day.setdefault(
            day,
            {"date": day, "steps": None, "energy_kcal": None, "weight_kg": None, "workouts": 0},
        )

    with conn.cursor() as cur:
        # Total steps per day (NEAT + per-workout, no double count — see view).
        cur.execute(
            """
            SELECT (time_bucket('1 day', started_at))::date AS day, sum(steps) AS steps
            FROM canonical.total_steps
            WHERE started_at >= %(start)s AND started_at < %(end)s
            GROUP BY day
            """,
            params,
        )
        for r in cur.fetchall():
            _row(r["day"])["steps"] = float(r["steps"]) if r["steps"] is not None else None

        # Total energy per day.
        cur.execute(
            """
            SELECT (time_bucket('1 day', started_at))::date AS day, sum(kcal) AS kcal
            FROM canonical.total_energy
            WHERE started_at >= %(start)s AND started_at < %(end)s
            GROUP BY day
            """,
            params,
        )
        for r in cur.fetchall():
            _row(r["day"])["energy_kcal"] = float(r["kcal"]) if r["kcal"] is not None else None

        # Mean body weight per day.
        cur.execute(
            """
            SELECT (time_bucket('1 day', valid_from))::date AS day, avg(value) AS kg
            FROM canonical.body_weight
            WHERE valid_from >= %(start)s AND valid_from < %(end)s
            GROUP BY day
            """,
            params,
        )
        for r in cur.fetchall():
            _row(r["day"])["weight_kg"] = float(r["kg"]) if r["kg"] is not None else None

        # Workout count per day.
        cur.execute(
            """
            SELECT (started_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n
            FROM raw.activities
            WHERE started_at >= %(start)s AND started_at < %(end)s
            GROUP BY day
            """,
            params,
        )
        for r in cur.fetchall():
            _row(r["day"])["workouts"] = int(r["n"])

    return [by_day[d] for d in sorted(by_day, reverse=True)]


# Shared header projection: raw.activities joined to per-workout canonical
# rollups. Used by both the list and the detail view.
_WORKOUT_HEADER_SELECT = """
    SELECT
        a.source,
        a.activity_uid,
        a.sport,
        a.program,
        a.device,
        a.started_at,
        a.ended_at,
        EXTRACT(EPOCH FROM (a.ended_at - a.started_at)) AS duration_s,
        wl.calories,
        wl.calories_is_derived,
        wl.training_load,
        ws.steps,
        ws.steps_is_derived
    FROM raw.activities a
    LEFT JOIN canonical.workout_load  wl ON wl.source = a.source AND wl.activity_uid = a.activity_uid
    LEFT JOIN canonical.workout_steps ws ON ws.source = a.source AND ws.activity_uid = a.activity_uid
"""


def _source_url(source: str, activity_uid: str) -> str | None:
    """Deep link back to the source's own detail page, where one exists.

    intervals.icu exposes a public activity page keyed by the same id we store
    as ``activity_uid``. Liftosaur has no per-workout URL, so it returns None.
    """
    if source == "intervals":
        return f"https://intervals.icu/activities/{activity_uid}"
    return None


def _header_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": r["source"],
        "activity_uid": r["activity_uid"],
        "source_url": _source_url(r["source"], r["activity_uid"]),
        "sport": r["sport"],
        "program": r["program"],
        "device": r["device"],
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "duration_s": int(r["duration_s"]) if r["duration_s"] is not None else None,
        "calories": float(r["calories"]) if r["calories"] is not None else None,
        "calories_is_derived": bool(r["calories_is_derived"]),
        "training_load": float(r["training_load"]) if r["training_load"] is not None else None,
        "steps": float(r["steps"]) if r["steps"] is not None else None,
        "steps_is_derived": bool(r["steps_is_derived"]),
    }


def list_workouts(
    conn: Connection,
    start: date | datetime,
    end: date | datetime,
    sport: str | None = None,
) -> list[dict[str, Any]]:
    """Workouts started in [start, end), newest first, with canonical calories/
    steps/training-load. Optional exact-match ``sport`` filter."""
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    params: dict[str, Any] = {"start": start_dt, "end": end_dt}
    sql = _WORKOUT_HEADER_SELECT + " WHERE a.started_at >= %(start)s AND a.started_at < %(end)s"
    if sport:
        sql += " AND a.sport = %(sport)s"
        params["sport"] = sport
    sql += " ORDER BY a.started_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_header_row(r) for r in cur.fetchall()]


def sports(conn: Connection, start: date | datetime, end: date | datetime) -> list[str]:
    """Distinct sports present in [start, end), for the list filter dropdown."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT sport FROM raw.activities
            WHERE started_at >= %(start)s AND started_at < %(end)s AND sport IS NOT NULL
            ORDER BY sport
            """,
            {"start": _as_utc(start), "end": _as_utc(end)},
        )
        return [r["sport"] for r in cur.fetchall()]


# Cap on stream points returned per metric; anything denser is bucket-averaged
# down so a multi-hour ride doesn't ship tens of thousands of points.
_STREAM_POINT_CAP = 3000


def workout_detail(
    conn: Connection, source: str, activity_uid: str
) -> dict[str, Any] | None:
    """Full detail for one workout: header, cardio stream series (downsampled),
    and the strength exercise/set hierarchy. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            _WORKOUT_HEADER_SELECT + " WHERE a.source = %(source)s AND a.activity_uid = %(uid)s",
            {"source": source, "uid": activity_uid},
        )
        head = cur.fetchone()
        if head is None:
            return None
        header = _header_row(head)

        streams = _load_streams(cur, source, activity_uid, header["duration_s"])
        exercises = _load_strength(cur, source, activity_uid)

    return {"header": header, "streams": streams, "exercises": exercises}


def _load_streams(
    cur: Any, source: str, activity_uid: str, duration_s: int | None
) -> dict[str, list[dict[str, float]]]:
    """Per-metric ``[{"t": epoch, "v": value}]`` for a cardio activity.

    Bucket-averages when a metric would exceed the point cap; otherwise returns
    the raw per-second stream."""
    # Choose a bucket wide enough to keep each metric under the cap. Streams are
    # ~1 Hz, so points ≈ duration_s; pick seconds-per-bucket = ceil(dur / cap).
    bucket_s = 1
    if duration_s and duration_s > _STREAM_POINT_CAP:
        bucket_s = -(-duration_s // _STREAM_POINT_CAP)  # ceil div
    cur.execute(
        """
        SELECT metric,
               time_bucket(make_interval(secs => %(bucket)s), t) AS bt,
               avg(value) AS v
        FROM raw.activity_streams
        WHERE source = %(source)s AND activity_uid = %(uid)s
        GROUP BY metric, bt
        ORDER BY metric, bt
        """,
        {"bucket": float(bucket_s), "source": source, "uid": activity_uid},
    )
    out: dict[str, list[dict[str, float]]] = {}
    for r in cur.fetchall():
        out.setdefault(r["metric"], []).append(
            {"t": int(r["bt"].timestamp()), "v": float(r["v"])}
        )
    return out


def _load_strength(cur: Any, source: str, activity_uid: str) -> list[dict[str, Any]]:
    """Exercises (in order) each with their sets, for a strength activity."""
    cur.execute(
        """
        SELECT e.exercise_uid, e.exercise_name, e.exercise_idx, e.is_unilateral,
               s.set_index, s.weight_kg, s.reps, s.left_reps, s.rpe, s.is_warmup
        FROM raw.strength_exercises e
        LEFT JOIN raw.strength_sets s
               ON s.source = e.source AND s.activity_uid = e.activity_uid
              AND s.exercise_uid = e.exercise_uid
        WHERE e.source = %(source)s AND e.activity_uid = %(uid)s
        ORDER BY e.exercise_idx, s.set_index
        """,
        {"source": source, "uid": activity_uid},
    )
    exercises: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in cur.fetchall():
        uid = r["exercise_uid"]
        if uid not in exercises:
            exercises[uid] = {
                "name": r["exercise_name"],
                "idx": r["exercise_idx"],
                "is_unilateral": bool(r["is_unilateral"]),
                "sets": [],
            }
            order.append(uid)
        if r["set_index"] is not None:
            exercises[uid]["sets"].append(
                {
                    "set_index": r["set_index"],
                    "weight_kg": float(r["weight_kg"]) if r["weight_kg"] is not None else None,
                    "reps": r["reps"],
                    "left_reps": r["left_reps"],
                    "rpe": float(r["rpe"]) if r["rpe"] is not None else None,
                    "is_warmup": bool(r["is_warmup"]),
                }
            )
    return [exercises[u] for u in order]
