"""Read-only queries over the reconciled ``canonical.*`` / ``derived.*`` layers.

Every function takes a psycopg connection (dict row factory) and returns plain
Python data ready for templating or JSON. Nothing here writes — the canonical
and derived views already resolve source precedence and rolling analytics, so
the UI reads them directly rather than re-implementing anything.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from psycopg import Connection

# --- metric registry -------------------------------------------------------
# Each surfaced metric maps to a view + its (date, value) columns. User-facing
# metric keys arrive via the URL and are validated against this dict, so the
# view/column names are never interpolated from untrusted input.
#
#   group   recovery | body | activity  (drives the Metrics-page sections)
#   better  high | low | None           (which direction is "good", for deltas)
METRICS: dict[str, dict[str, Any]] = {
    "hrv": {
        "label": "HRV", "desc": "Heart-rate variability", "unit": "ms", "group": "recovery",
        "color": "#34d3c2", "view": "canonical.hrv_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "band": {
            "view": "derived.hrv_status", "date_col": "local_date",
            "lo": "low_thresh", "hi": "high_thresh",
        },
    },
    "resting_heart_rate": {
        "label": "Resting HR", "desc": "Overnight resting heart rate", "unit": "bpm",
        "group": "recovery", "color": "#5aa9ff", "view": "canonical.resting_heart_rate",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "low",
    },
    "respiratory_rate": {
        "label": "Respiratory rate", "desc": "Overnight breaths/min", "unit": "br/min",
        "group": "recovery", "color": "#4bc8c2", "view": "canonical.respiratory_rate",
        "date_col": "local_date", "value_col": "value", "digits": 1, "better": None,
        "status": "respiratory_rate",
    },
    "spo2": {
        "label": "SpO₂", "desc": "Overnight blood oxygen", "unit": "%", "group": "recovery",
        "color": "#4bc8c2", "view": "canonical.spo2_daily",
        "date_col": "local_date", "value_col": "spo2_avg", "digits": 0, "better": "high",
    },
    "skin_temp": {
        "label": "Skin temperature", "desc": "Nightly baseline deviation", "unit": "°C Δ",
        "group": "recovery",
        "color": "#59b6d6", "view": "canonical.skin_temp",
        "date_col": "valid_from", "value_col": "value", "digits": 1, "better": None,
    },
    "body_weight": {
        "label": "Body weight", "desc": "Withings", "unit": "lb", "group": "body",
        "color": "#6dd0b0", "view": "canonical.body_weight",
        "date_col": "valid_from", "value_col": "value", "digits": 1, "better": "low",
        "trend": "body_composition_trend", "display_scale": 2.2046226218,
    },
    "body_fat_ratio": {
        "label": "Body fat", "desc": "Withings", "unit": "%", "group": "body",
        "color": "#6dd0b0", "view": "canonical.body_fat_ratio",
        "date_col": "valid_from", "value_col": "value", "digits": 1, "better": "low",
        "trend": "body_composition_trend",
    },
    "muscle_mass": {
        "label": "Muscle mass", "desc": "Withings", "unit": "lb", "group": "body",
        "color": "#6dd0b0", "view": "canonical.muscle_mass",
        "date_col": "valid_from", "value_col": "value", "digits": 1, "better": "high",
        "trend": "body_composition_trend", "display_scale": 2.2046226218,
    },
    "fat_free_mass": {
        "label": "Fat-free mass", "desc": "Withings", "unit": "lb", "group": "body",
        "color": "#6dd0b0", "view": "canonical.fat_free_mass",
        "date_col": "valid_from", "value_col": "value", "digits": 1, "better": "high",
        "trend": "body_composition_trend", "display_scale": 2.2046226218,
    },
    "steps": {
        "label": "Steps", "desc": "Daily total", "unit": "", "group": "activity",
        "color": "#3fd6a0", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'steps' AND kind = 'total'",
    },
    "steps_neat": {
        "label": "NEAT steps", "desc": "Outside recorded workouts", "unit": "",
        "group": "activity", "color": "#57b88f", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'steps' AND kind = 'neat'",
    },
    "steps_workout": {
        "label": "Exercise steps", "desc": "Recorded workouts", "unit": "",
        "group": "activity", "color": "#72e6b9", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'steps' AND kind = 'workout'",
    },
    "active_calories": {
        "label": "Active calories", "desc": "Daily total", "unit": "kcal",
        "group": "activity", "color": "#f0a63c", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'active_calories' AND kind = 'total'",
    },
    "active_calories_neat": {
        "label": "NEAT calories", "desc": "Outside recorded workouts", "unit": "kcal",
        "group": "activity", "color": "#c68d42", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'active_calories' AND kind = 'neat'",
    },
    "active_calories_workout": {
        "label": "Exercise calories", "desc": "Recorded workouts", "unit": "kcal",
        "group": "activity", "color": "#f6bd62", "view": "canonical.activity_daily",
        "date_col": "local_date", "value_col": "value", "digits": 0, "better": "high",
        "where": "metric = 'active_calories' AND kind = 'workout'",
    },
    "form": {
        "label": "Form (TSB)", "desc": "Fitness − fatigue", "unit": "", "group": "activity",
        "color": "#f0a63c", "view": "derived.pmc",
        "date_col": "local_date", "value_col": "form", "digits": 0, "better": "high",
    },
}

SLEEP_GOAL_MIN = 480  # 8h reference for the ring


def _as_utc(d: date | datetime) -> datetime:
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _bucket_for_range(start: datetime, end: datetime) -> str:
    span_days = (end - start).total_seconds() / 86400.0
    if span_days <= 2:
        return "1 minute"
    if span_days <= 60:
        return "1 hour"
    return "1 day"


# --- generic per-metric helpers -------------------------------------------


def _where(m: dict) -> str:
    return f"AND {m['where']}" if m.get("where") else ""


def _value_expr(m: dict) -> str:
    value = m["value_col"]
    scale = m.get("display_scale")
    return f"({value} * {scale})" if scale is not None else value


def _spark_points(values: list[float], width: int = 60, height: int = 24, pad: int = 3) -> str:
    """SVG polyline points for a sparkline. Flat line if all-equal / single point."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    n = len(values)
    step = (width - 2 * pad) / (n - 1) if n > 1 else 0
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = height - pad - (v - lo) / span * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _daily_values(conn: Connection, m: dict, days: int = 14) -> list[float]:
    """Trailing per-day values for a sparkline, oldest→newest."""
    sql = f"""
        SELECT avg({_value_expr(m)}) AS v
        FROM {m['view']}
        WHERE {m['date_col']} >= (now() - make_interval(days => %(days)s)) {_where(m)}
        GROUP BY date_trunc('day', {m['date_col']})
        ORDER BY date_trunc('day', {m['date_col']})
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"days": days})
        return [float(r["v"]) for r in cur.fetchall() if r["v"] is not None]


def _latest(conn: Connection, m: dict) -> dict[str, Any] | None:
    """Latest value + a 7-day mean of the preceding readings, for a delta."""
    sql = f"""
        WITH s AS (
            SELECT {m['date_col']} AS d, {_value_expr(m)} AS v
            FROM {m['view']}
            WHERE {m['value_col']} IS NOT NULL {_where(m)}
            ORDER BY {m['date_col']} DESC
            LIMIT 30
        )
        SELECT
            (SELECT v FROM s ORDER BY d DESC LIMIT 1) AS latest,
            (SELECT d FROM s ORDER BY d DESC LIMIT 1) AS latest_at,
            (SELECT avg(v) FROM (SELECT v FROM s ORDER BY d DESC OFFSET 1) q) AS baseline
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        r = cur.fetchone()
    if not r or r["latest"] is None:
        return None
    latest = float(r["latest"])
    baseline = float(r["baseline"]) if r["baseline"] is not None else None
    delta = latest - baseline if baseline is not None else None
    return {"value": latest, "at": r["latest_at"], "baseline": baseline, "delta": delta}


def _card(conn: Connection, key: str, spark_days: int = 14) -> dict[str, Any] | None:
    """Full display bundle for one metric: latest, delta, spark points, meta."""
    m = METRICS[key]
    latest = _latest(conn, m)
    if latest is None:
        return None
    vals = _daily_values(conn, m, spark_days)
    better = m.get("better")
    delta = latest["delta"]
    dir_ = "flat"
    if delta is not None and abs(delta) >= (0.05 if m["digits"] else 0.5):
        rising = delta > 0
        if better is None:
            dir_ = "up" if rising else "down"
        else:
            good = (rising and better == "high") or (not rising and better == "low")
            dir_ = "down" if good else "up"  # 'down' class = good/teal, 'up' = neutral teal
    per_week = _body_slope_per_week(conn, key) if m.get("trend") == "body_composition_trend" else None
    status = _metric_status(conn, m.get("status"))
    return {
        "key": key, "label": m["label"], "desc": m["desc"], "unit": m["unit"],
        "color": m["color"], "group": m["group"], "digits": m["digits"],
        "value": latest["value"], "at": latest["at"], "delta": delta, "dir": dir_,
        "per_week": per_week, "spark": _spark_points(vals), "status": status,
    }


def _body_slope_per_week(conn: Connection, metric: str) -> float | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT slope_per_week
            FROM derived.body_composition_trend
            WHERE metric = %(metric)s
            ORDER BY valid_from DESC
            LIMIT 1
        """, {"metric": metric})
        r = cur.fetchone()
    if not r or r["slope_per_week"] is None:
        return None
    return float(r["slope_per_week"]) * METRICS[metric].get("display_scale", 1.0)


def _metric_status(conn: Connection, status: str | None) -> str | None:
    if status != "respiratory_rate":
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT base_n, elevated
            FROM derived.respiratory_rate_status
            ORDER BY local_date DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    if not row or row["base_n"] < 14:
        return "pending"
    return "elevated" if row["elevated"] else "normal"


def blood_pressure_row(conn: Connection) -> dict[str, Any] | None:
    """Latest BP plus a trailing systolic sparkline for the Metrics index."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT valid_from, systolic, diastolic
            FROM canonical.blood_pressure
            ORDER BY valid_from DESC
            LIMIT 1
        """)
        latest = cur.fetchone()
        if latest is None:
            return None
        cur.execute("""
            SELECT avg(systolic) AS v
            FROM canonical.blood_pressure
            WHERE valid_from >= (now() - make_interval(days => 14))
              AND systolic IS NOT NULL
            GROUP BY date_trunc('day', valid_from)
            ORDER BY date_trunc('day', valid_from)
        """)
        vals = [float(r["v"]) for r in cur.fetchall() if r["v"] is not None]
    return {
        "latest_systolic": float(latest["systolic"]) if latest["systolic"] is not None else None,
        "latest_diastolic": float(latest["diastolic"]) if latest["diastolic"] is not None else None,
        "at": latest["valid_from"],
        "spark": _spark_points(vals),
    }


# --- home ------------------------------------------------------------------


def home(conn: Connection) -> dict[str, Any]:
    """Everything the 1a sleep-hero home needs. Any piece may be None (no data
    yet), and the template renders a graceful empty state for it."""
    out: dict[str, Any] = {}

    # Sleep hero: latest night + 14d duration spark.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT e.started_at, e.ended_at, e.started_local, e.ended_local,
                   e.minutes_asleep, e.efficiency_pct, r.sri
            FROM derived.sleep_efficiency e
            LEFT JOIN derived.sleep_regularity r
                   ON r.as_of_date = (e.started_at AT TIME ZONE 'UTC')::date
            ORDER BY e.started_at DESC LIMIT 1
        """)
        out["sleep"] = cur.fetchone()

    out["hrv"] = _card(conn, "hrv")
    out["rhr"] = _card(conn, "resting_heart_rate")
    out["weight"] = _card(conn, "body_weight")
    out["body_fat"] = _card(conn, "body_fat_ratio")
    out["steps"] = _card(conn, "steps")

    # HRV / RHR status chips.
    out["hrv_status"] = _status(conn, "derived.hrv_status", "status")
    out["rhr_status"] = _status(conn, "derived.rhr_status", "status")

    # PMC form.
    with conn.cursor() as cur:
        cur.execute("SELECT local_date, ctl, atl, form FROM derived.pmc ORDER BY local_date DESC LIMIT 1")
        out["pmc"] = cur.fetchone()

    # Blood pressure (latest).
    with conn.cursor() as cur:
        cur.execute("SELECT valid_from, systolic, diastolic FROM canonical.blood_pressure ORDER BY valid_from DESC LIMIT 1")
        out["bp"] = cur.fetchone()

    out["workouts"] = list_workouts_recent(conn, limit=3)
    return out


def _status(conn: Connection, view: str, col: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {col} AS s FROM {view} ORDER BY local_date DESC LIMIT 1")  # noqa: S608
        r = cur.fetchone()
    return r["s"] if r else None


# --- metrics index (1e) ----------------------------------------------------


def metric_index(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Cards for every metric, grouped for the Metrics page."""
    groups: dict[str, list[dict[str, Any]]] = {"recovery": [], "body": [], "activity": []}
    for key, m in METRICS.items():
        card = _card(conn, key)
        if card is None:
            card = {"key": key, "label": m["label"], "desc": m["desc"], "unit": m["unit"],
                    "color": m["color"], "group": m["group"], "digits": m["digits"],
                    "value": None, "delta": None, "dir": "flat", "per_week": None, "spark": ""}
        groups[m["group"]].append(card)
    bp = blood_pressure_row(conn)
    if bp is not None:
        groups["body"].append({
            "key": "blood_pressure", "label": "Blood pressure", "desc": "Withings",
            "unit": "mmHg", "color": "#6dd0b0", "group": "body", "bp": True,
            "systolic": bp["latest_systolic"], "diastolic": bp["latest_diastolic"],
            "spark": bp["spark"], "href": "/metrics/blood_pressure",
        })
    return groups


# --- metric detail (1d) ----------------------------------------------------


def metric_series(conn: Connection, metric: str, start: date | datetime, end: date | datetime) -> dict[str, Any]:
    """Bucketed avg/min/max series for one metric over [start, end)."""
    m = METRICS[metric]
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    bucket = _bucket_for_range(start_dt, end_dt)
    dcol = m["date_col"]
    sql = f"""
        SELECT time_bucket(%(bucket)s::interval, {dcol}::timestamptz) AS t,
               avg({_value_expr(m)}) AS avg,
               min({_value_expr(m)}) AS min,
               max({_value_expr(m)}) AS max
        FROM {m['view']}
        WHERE {dcol}::timestamptz >= %(start)s AND {dcol}::timestamptz < %(end)s {_where(m)}
        GROUP BY t ORDER BY t
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"bucket": bucket, "start": start_dt, "end": end_dt})
        rows = cur.fetchall()
    points = [
        {"t": int(r["t"].timestamp()),
         "avg": float(r["avg"]) if r["avg"] is not None else None,
         "min": float(r["min"]) if r["min"] is not None else None,
         "max": float(r["max"]) if r["max"] is not None else None}
        for r in rows
    ]
    out = {"metric": metric, "label": m["label"], "unit": m["unit"], "bucket": bucket, "points": points}
    if band := m.get("band"):
        bdcol = band["date_col"]
        band_sql = f"""
            SELECT time_bucket(%(bucket)s::interval, {bdcol}::timestamptz) AS t,
                   avg({band['lo']}) AS lo, avg({band['hi']}) AS hi
            FROM {band['view']}
            WHERE {bdcol}::timestamptz >= %(start)s
              AND {bdcol}::timestamptz < %(end)s
              AND {band['lo']} IS NOT NULL
              AND {band['hi']} IS NOT NULL
            GROUP BY t ORDER BY t
        """
        with conn.cursor() as cur:
            cur.execute(band_sql, {"bucket": bucket, "start": start_dt, "end": end_dt})
            band_rows = cur.fetchall()
        out["band"] = [
            {"t": int(r["t"].timestamp()), "lo": float(r["lo"]), "hi": float(r["hi"])}
            for r in band_rows
            if r["lo"] is not None and r["hi"] is not None
        ]
    return out


def blood_pressure_detail(conn: Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT valid_from, systolic, diastolic
            FROM canonical.blood_pressure
            WHERE systolic IS NOT NULL AND diastolic IS NOT NULL
            ORDER BY valid_from DESC
            LIMIT 12
        """)
        rows = cur.fetchall()
    recent = [
        {
            "valid_from": r["valid_from"],
            "systolic": float(r["systolic"]),
            "diastolic": float(r["diastolic"]),
        }
        for r in rows
    ]
    return {"latest": recent[0] if recent else None, "recent": recent}


def metric_detail(conn: Connection, metric: str) -> dict[str, Any]:
    """Header stats + recent readings for the metric detail page."""
    m = METRICS[metric]
    card = _card(conn, metric, spark_days=30)
    # 7-day / range stats.
    sql = f"""
        SELECT avg(v) AS avg7, min(v) AS lo, max(v) AS hi FROM (
            SELECT {_value_expr(m)} AS v FROM {m['view']}
            WHERE {m['value_col']} IS NOT NULL {_where(m)}
            ORDER BY {m['date_col']} DESC LIMIT 7
        ) q
    """
    recent_sql = f"""
        SELECT {m['date_col']} AS d, {_value_expr(m)} AS v FROM {m['view']}
        WHERE {m['value_col']} IS NOT NULL {_where(m)}
        ORDER BY {m['date_col']} DESC LIMIT 8
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        stats = cur.fetchone() or {}
        cur.execute(recent_sql)
        recent = cur.fetchall()
    lo = float(stats["lo"]) if stats.get("lo") is not None else None
    hi = float(stats["hi"]) if stats.get("hi") is not None else None
    return {
        "meta": m, "metric": metric, "card": card,
        "avg7": float(stats["avg7"]) if stats.get("avg7") is not None else None,
        "lo": lo, "hi": hi,
        "recent": [{"d": r["d"], "v": float(r["v"])} for r in recent],
        "status": card.get("status") if card else None,
    }


# --- sleep detail (2a) -----------------------------------------------------


def sleep_detail(conn: Connection) -> dict[str, Any] | None:
    """Latest night: session, hypnogram stages, composition, efficiency, SRI."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.session_uid, s.source, s.started_at, s.ended_at, s.minutes_asleep,
                   s.minutes_awake, s.efficiency,
                   e.started_local, e.ended_local,
                   e.efficiency_pct, e.time_in_bed_min, e.efficiency_pct_avg_7d,
                   c.total_min, c.deep_min, c.rem_min, c.light_min, c.awake_min,
                   c.pct_deep, c.pct_rem, c.pct_light, c.pct_awake,
                   r.sri, r.nights AS sri_nights
            FROM canonical.sleep s
            JOIN derived.sleep_efficiency e ON e.started_at = s.started_at
            LEFT JOIN derived.sleep_stage_composition c ON c.session_uid = s.session_uid
            LEFT JOIN derived.sleep_regularity r
                   ON r.as_of_date = (s.started_at AT TIME ZONE 'UTC')::date
            ORDER BY s.started_at DESC LIMIT 1
        """)
        night = cur.fetchone()
        if night is None:
            return None
        cur.execute("""
            SELECT stage, started_at, ended_at,
                   EXTRACT(EPOCH FROM (ended_at - started_at)) AS dur_s
            FROM canonical.sleep_stages
            WHERE source = %(src)s AND session_uid = %(uid)s
            ORDER BY started_at
        """, {"src": night["source"], "uid": night["session_uid"]})
        stages = cur.fetchall()
    # hypnogram geometry: reserve the left side for lane labels, then map each
    # segment into the remaining plot range.
    t0 = night["started_at"].timestamp()
    total = max(night["ended_at"].timestamp() - t0, 1.0)
    plot_left = 18.0
    plot_width = 100.0 - plot_left
    lanes = {"AWAKE": 0, "REM": 1, "LIGHT": 2, "DEEP": 3}
    segs = []
    for st in stages:
        x = plot_left + (st["started_at"].timestamp() - t0) / total * plot_width
        w = float(st["dur_s"]) / total * plot_width
        segs.append({"stage": st["stage"], "x": x, "w": max(w, 0.4),
                     "lane": lanes.get(st["stage"].upper(), 2)})
    return {"night": night, "segments": segs}


# --- workouts (unchanged behaviour) ---------------------------------------

_WORKOUT_HEADER_SELECT = """
    SELECT
        a.source, a.activity_uid, a.sport, a.program, a.device,
        a.started_at, a.ended_at,
        EXTRACT(EPOCH FROM (a.ended_at - a.started_at)) AS duration_s,
        wl.calories, wl.calories_is_derived, wl.calories_method, wl.training_load,
        ws.steps, ws.steps_is_derived
    FROM raw.activities a
    LEFT JOIN canonical.workout_load  wl ON wl.source = a.source AND wl.activity_uid = a.activity_uid
    LEFT JOIN canonical.workout_steps ws ON ws.source = a.source AND ws.activity_uid = a.activity_uid
"""


def _source_url(source: str, activity_uid: str) -> str | None:
    if source == "intervals":
        return f"https://intervals.icu/activities/{activity_uid}"
    return None


def _is_strength(sport: str | None) -> bool:
    s = (sport or "").lower()
    return any(k in s for k in ("strength", "weight", "lift"))


def _header_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": r["source"], "activity_uid": r["activity_uid"],
        "source_url": _source_url(r["source"], r["activity_uid"]),
        "sport": r["sport"], "program": r["program"], "device": r["device"],
        "started_at": r["started_at"], "ended_at": r["ended_at"],
        "duration_s": int(r["duration_s"]) if r["duration_s"] is not None else None,
        "calories": float(r["calories"]) if r["calories"] is not None else None,
        "calories_is_derived": bool(r["calories_is_derived"]),
        "calories_method": r.get("calories_method"),
        "training_load": float(r["training_load"]) if r["training_load"] is not None else None,
        "steps": float(r["steps"]) if r["steps"] is not None else None,
        "steps_is_derived": bool(r["steps_is_derived"]),
        "is_strength": _is_strength(r["sport"]),
    }


def list_workouts(conn: Connection, start: date | datetime, end: date | datetime, sport: str | None = None) -> list[dict[str, Any]]:
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


def list_workouts_recent(conn: Connection, limit: int = 3) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_WORKOUT_HEADER_SELECT + " ORDER BY a.started_at DESC LIMIT %(n)s", {"n": limit})
        return [_header_row(r) for r in cur.fetchall()]


def sports(conn: Connection, start: date | datetime, end: date | datetime) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT sport FROM raw.activities
            WHERE started_at >= %(start)s AND started_at < %(end)s AND sport IS NOT NULL
            ORDER BY sport
        """, {"start": _as_utc(start), "end": _as_utc(end)})
        return [r["sport"] for r in cur.fetchall()]


_STREAM_POINT_CAP = 3000


def workout_detail(conn: Connection, source: str, activity_uid: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_WORKOUT_HEADER_SELECT + " WHERE a.source = %(source)s AND a.activity_uid = %(uid)s",
                    {"source": source, "uid": activity_uid})
        head = cur.fetchone()
        if head is None:
            return None
        header = _header_row(head)
        streams = _load_streams(cur, source, activity_uid, header["duration_s"])
        exercises = _load_strength(cur, source, activity_uid)
    return {"header": header, "streams": streams, "exercises": exercises}


def _load_streams(cur: Any, source: str, activity_uid: str, duration_s: int | None) -> dict[str, list[dict[str, float]]]:
    bucket_s = 1
    if duration_s and duration_s > _STREAM_POINT_CAP:
        bucket_s = -(-duration_s // _STREAM_POINT_CAP)
    cur.execute("""
        SELECT metric, time_bucket(make_interval(secs => %(bucket)s), t) AS bt, avg(value) AS v
        FROM raw.activity_streams
        WHERE source = %(source)s AND activity_uid = %(uid)s
        GROUP BY metric, bt ORDER BY metric, bt
    """, {"bucket": float(bucket_s), "source": source, "uid": activity_uid})
    out: dict[str, list[dict[str, float]]] = {}
    for r in cur.fetchall():
        out.setdefault(r["metric"], []).append({"t": int(r["bt"].timestamp()), "v": float(r["v"])})
    return out


def _load_strength(cur: Any, source: str, activity_uid: str) -> list[dict[str, Any]]:
    cur.execute("""
        SELECT e.exercise_uid, e.exercise_name, e.exercise_idx, e.is_unilateral,
               s.set_index, s.weight_kg, s.reps, s.left_reps, s.rpe, s.is_warmup
        FROM raw.strength_exercises e
        LEFT JOIN raw.strength_sets s
               ON s.source = e.source AND s.activity_uid = e.activity_uid AND s.exercise_uid = e.exercise_uid
        WHERE e.source = %(source)s AND e.activity_uid = %(uid)s
        -- Warmups first: they're performed before the working sets, but the
        -- Liftohistory text lists them after (`... / 3x8 120lb / warmup: ...`),
        -- so set_index alone puts them last. Sorting here rather than at ingest
        -- keeps set_index faithful to the source and fixes already-stored rows.
        ORDER BY e.exercise_idx, s.is_warmup DESC, s.set_index
    """, {"source": source, "uid": activity_uid})
    exercises: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in cur.fetchall():
        uid = r["exercise_uid"]
        if uid not in exercises:
            exercises[uid] = {"name": r["exercise_name"], "idx": r["exercise_idx"],
                              "is_unilateral": bool(r["is_unilateral"]), "sets": []}
            order.append(uid)
        if r["set_index"] is not None:
            exercises[uid]["sets"].append({
                "set_index": r["set_index"],
                "weight_kg": float(r["weight_kg"]) if r["weight_kg"] is not None else None,
                "reps": r["reps"], "left_reps": r["left_reps"],
                "rpe": float(r["rpe"]) if r["rpe"] is not None else None,
                "is_warmup": bool(r["is_warmup"]),
            })
    return [exercises[u] for u in order]


# --- monitoring ------------------------------------------------------------


def ingest_freshness(conn: Connection) -> list[dict[str, Any]]:
    """Per-source ingest freshness for the Prometheus /-/metrics endpoint:
    the epoch of the most recent ingested row and the lag since. Reads
    derived.ingest_freshness (see migration 0020)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source,
                   extract(epoch FROM last_ingest_at)::bigint AS last_ingest_epoch,
                   lag_seconds
            FROM derived.ingest_freshness
            ORDER BY source
        """)
        return cur.fetchall()
