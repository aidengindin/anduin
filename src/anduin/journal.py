"""Headache journal: validation + persistence for user-entered check-ins.

The one manual-logging surface after the weight goal. A *check-in* is "how is
my head right now" -- a timestamp and a 0-10 intensity with optional symptom
detail -- not an attack with a start and an end. A day is a small series of
check-ins; peak, mean and the rest are derived (``derived.headache_daily``).

Lives outside ``web/`` because the ntfy reminder (a CLI oneshot) reads the
latest check-in and must not import FastAPI. Every function takes a psycopg
connection with a **dict row factory** and relies on the caller's autocommit
(the web pool's, or ``db.connect_dict`` for the CLI), like ``web/goals.py``.

See docs/plans/2026-09-01-headache-log-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from psycopg import Connection

QUALITIES = ("pressure", "throbbing", "sharp", "icepick", "unilateral")
FLUORESCENT = ("none", "brief", "hours")
SOURCES = ("app", "ntfy")
INTENSITY_MAX = 10
NAUSEA_MAX = 3
NOTE_MAX = 500
INTAKE_MAX = 20   # cups / drinks per day; anything above is a typo

# A check-in typed in for a moment more than this far back is almost certainly
# a mis-set picker; the future is never right (a small allowance for clock skew).
BACKFILL_MAX_DAYS = 30
FUTURE_SLACK = timedelta(minutes=5)


class JournalError(ValueError):
    """Invalid input. The message is shown to the user inline."""


@dataclass
class Checkin:
    logged_at: datetime            # aware, UTC
    tz_offset_minutes: int         # minutes east of UTC where the owner was
    local_date: date               # civil date at logged_at in that zone
    intensity: int
    nausea: int = 0
    light: bool = False
    noise: bool = False
    qualities: list[str] = field(default_factory=list)
    note: str | None = None
    source: str = "app"


# --- parsing -----------------------------------------------------------------


def _getlist(form: Mapping[str, Any], key: str) -> list[str]:
    getlist = getattr(form, "getlist", None)
    if getlist is not None:
        return [str(v) for v in getlist(key)]
    v = form.get(key)
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]


def _int(form: Mapping[str, Any], key: str, lo: int, hi: int, *, default: int | None = None) -> int:
    raw = form.get(key)
    if raw is None or str(raw).strip() == "":
        if default is None:
            raise JournalError(f"{key} is required")
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise JournalError(f"{key} must be a whole number") from None
    if not lo <= v <= hi:
        raise JournalError(f"{key} must be between {lo} and {hi}")
    return v


def _note(form: Mapping[str, Any], key: str = "note") -> str | None:
    raw = form.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    if len(text) > NOTE_MAX:
        raise JournalError(f"note must be {NOTE_MAX} characters or fewer")
    return text or None


def local_date_of(at_utc: datetime, tz_offset_minutes: int) -> date:
    """Civil date at ``at_utc`` in a zone ``tz_offset_minutes`` east of UTC."""
    return (at_utc + timedelta(minutes=tz_offset_minutes)).date()


def parse_checkin(
    form: Mapping[str, Any], *, now: datetime, server_offset_minutes: int
) -> Checkin:
    """Validate a submitted check-in form (or the ntfy button's bare POST).

    ``at`` is a ``datetime-local`` string -- wall time in the *browser's* zone,
    carried separately as ``tz_offset`` (minutes east of UTC). Neither is
    required: an empty ``at`` means now, and a missing offset (the ntfy path has
    no browser) falls back to the server's zone.
    """
    intensity = _int(form, "intensity", 0, INTENSITY_MAX)
    nausea = _int(form, "nausea", 0, NAUSEA_MAX, default=0)
    qualities = _getlist(form, "qualities")
    unknown = [q for q in qualities if q not in QUALITIES]
    if unknown:
        raise JournalError(f"unknown quality: {unknown[0]}")
    source = str(form.get("source") or "app")
    if source not in SOURCES:
        raise JournalError(f"unknown source: {source}")

    raw_offset = form.get("tz_offset")
    if raw_offset is None or str(raw_offset).strip() == "":
        offset = server_offset_minutes
    else:
        try:
            offset = int(str(raw_offset).strip())
        except ValueError:
            raise JournalError("tz_offset must be minutes") from None
        if not -14 * 60 <= offset <= 14 * 60:
            raise JournalError("tz_offset out of range")

    raw_at = form.get("at")
    if raw_at is None or str(raw_at).strip() == "":
        at_utc = now.astimezone(timezone.utc)
    else:
        try:
            wall = datetime.fromisoformat(str(raw_at).strip())
        except ValueError:
            raise JournalError("time must be a date and time") from None
        if wall.tzinfo is not None:
            at_utc = wall.astimezone(timezone.utc)
        else:
            at_utc = (wall - timedelta(minutes=offset)).replace(tzinfo=timezone.utc)
        if at_utc > now + FUTURE_SLACK:
            raise JournalError("that time is in the future")
        if at_utc < now - timedelta(days=BACKFILL_MAX_DAYS):
            raise JournalError(f"that time is more than {BACKFILL_MAX_DAYS} days ago")

    return Checkin(
        logged_at=at_utc,
        tz_offset_minutes=offset,
        local_date=local_date_of(at_utc, offset),
        intensity=intensity,
        nausea=nausea,
        light=form.get("light") is not None,
        noise=form.get("noise") is not None,
        qualities=list(dict.fromkeys(qualities)),
        note=_note(form),
        source=source,
    )


def parse_day_context(form: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the per-day context form into the columns it sets.

    Only submitted fields come back, so the fluorescent buttons, the day-peak
    row and the note can each be their own one-tap form without clobbering
    the others. An empty ``peak`` clears the override (NULL).
    """
    out: dict[str, Any] = {}
    if "fluorescent" in form:
        v = str(form.get("fluorescent"))
        if v not in FLUORESCENT:
            raise JournalError(f"unknown fluorescent exposure: {v}")
        out["fluorescent_exposure"] = v
    if "peak" in form:
        raw = str(form.get("peak") or "").strip()
        out["peak_intensity"] = _int(form, "peak", 0, INTENSITY_MAX) if raw else None
    for key, col in (("coffee", "coffee_cups"), ("alcohol", "alcohol_drinks")):
        if key in form:
            raw = str(form.get(key) or "").strip()
            out[col] = _int(form, key, 0, INTAKE_MAX) if raw else None
    # "daynote", because the check-in form already uses "note" and the two
    # must never be confused on the wire.
    if "daynote" in form:
        out["note"] = _note(form, "daynote")
    if not out:
        raise JournalError("nothing to save")
    return out


# --- persistence -------------------------------------------------------------


def add_checkin(conn: Connection, user_id: int, c: Checkin) -> int | None:
    """Insert one check-in. Returns the new id, or None if a row already
    existed at that instant (a double-tapped ntfy button), which is not an error."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO journal.headache_checkins
                (user_id, logged_at, tz_offset_minutes, local_date, intensity, nausea,
                 light_sensitivity, noise_sensitivity, qualities, note, source)
            VALUES
                (%(user_id)s, %(logged_at)s, %(tz_offset_minutes)s, %(local_date)s,
                 %(intensity)s, %(nausea)s, %(light)s, %(noise)s, %(qualities)s,
                 %(note)s, %(source)s)
            ON CONFLICT (user_id, logged_at) DO NOTHING
            RETURNING id
        """, {
            "user_id": user_id, "logged_at": c.logged_at,
            "tz_offset_minutes": c.tz_offset_minutes, "local_date": c.local_date,
            "intensity": c.intensity, "nausea": c.nausea, "light": c.light,
            "noise": c.noise, "qualities": c.qualities, "note": c.note,
            "source": c.source,
        })
        row = cur.fetchone()
    return int(row["id"]) if row else None


def delete_checkin(conn: Connection, user_id: int, checkin_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM journal.headache_checkins WHERE user_id = %(user_id)s AND id = %(id)s",
            {"user_id": user_id, "id": checkin_id},
        )


def checkins_for_day(conn: Connection, user_id: int, local_date: date) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, logged_at, tz_offset_minutes, intensity, nausea,
                   light_sensitivity, noise_sensitivity, qualities, note, source
            FROM journal.headache_checkins
            WHERE user_id = %(user_id)s AND local_date = %(local_date)s
            ORDER BY logged_at
        """, {"user_id": user_id, "local_date": local_date})
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["local_time"] = r["logged_at"] + timedelta(minutes=r["tz_offset_minutes"])
        out.append(d)
    return out


def latest_checkin_at(conn: Connection, user_id: int) -> datetime | None:
    """When the owner last answered, for the reminder's skip window."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT logged_at FROM journal.headache_checkins
            WHERE user_id = %(user_id)s
            ORDER BY logged_at DESC LIMIT 1
        """, {"user_id": user_id})
        row = cur.fetchone()
    return row["logged_at"] if row else None


def day_context(conn: Connection, user_id: int, local_date: date) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fluorescent_exposure, peak_intensity, coffee_cups, alcohol_drinks, note
            FROM journal.headache_days
            WHERE user_id = %(user_id)s AND local_date = %(local_date)s
        """, {"user_id": user_id, "local_date": local_date})
        row = cur.fetchone()
    return dict(row) if row else None


_DAY_COLUMNS = ("fluorescent_exposure", "peak_intensity", "coffee_cups", "alcohol_drinks", "note")


def set_day_context(
    conn: Connection, user_id: int, local_date: date, fields: dict[str, Any]
) -> None:
    """Upsert the day's context, touching only the columns in ``fields``."""
    bad = [k for k in fields if k not in _DAY_COLUMNS]
    if bad or not fields:
        raise ValueError(f"unknown day-context columns: {bad}")
    cols = list(fields)
    sql = f"""
        INSERT INTO journal.headache_days (user_id, local_date, {", ".join(cols)})
        VALUES (%(user_id)s, %(local_date)s, {", ".join(f"%({c})s" for c in cols)})
        ON CONFLICT (user_id, local_date) DO UPDATE
            SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)},
                updated_at = now()
    """  # noqa: S608 -- column names come from the whitelist above
    with conn.cursor() as cur:
        cur.execute(sql, {"user_id": user_id, "local_date": local_date, **fields})


def recent_days(conn: Connection, user_id: int, days: int = 14) -> list[dict[str, Any]]:
    """The last ``days`` days that have anything logged, newest first."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT local_date, n_checkins, checkin_peak, day_peak, peak,
                   mean_intensity, fluorescent_exposure, coffee_cups, alcohol_drinks
            FROM derived.headache_daily
            WHERE user_id = %(user_id)s
              AND local_date >= current_date - make_interval(days => %(days)s)
            ORDER BY local_date DESC
        """, {"user_id": user_id, "days": days})
        return [dict(r) for r in cur.fetchall()]


# --- display helpers ---------------------------------------------------------


def timeline_points(
    rows: list[dict[str, Any]], *, width: int = 240, height: int = 40, pad: int = 4
) -> str:
    """SVG polyline points for a day's check-ins, x = time of day, y = intensity
    on a fixed 0-10 scale (so two days are comparable at a glance)."""
    pts = []
    for r in rows:
        local = r["logged_at"] + timedelta(minutes=r["tz_offset_minutes"])
        minutes = local.hour * 60 + local.minute
        x = minutes / 1440 * width
        y = height - pad - (r["intensity"] / INTENSITY_MAX) * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)
