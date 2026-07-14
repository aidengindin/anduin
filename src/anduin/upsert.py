"""Idempotent upsert helpers for raw tables.

Each helper takes a psycopg connection and a list of dicts, and writes them in
a single executemany. Restatement (raw payload changed) is handled by an ON
UPDATE trigger that writes to raw.restatements.
"""

from __future__ import annotations

from typing import Iterable

from psycopg import Connection
from psycopg.types.json import Jsonb


# Single source of truth for the raw.activities header upsert, shared by both
# the cardio path (upsert_activity) and the strength path (upsert_strength).
# Covers every column so a schema change only has to be made here once.
_ACTIVITY_HEADER_SQL = """
INSERT INTO raw.activities
    (source, activity_uid, device, recording_method, sport, program,
     started_at, ended_at, summary, raw, natural_key)
VALUES
    (%(source)s, %(activity_uid)s, %(device)s, %(recording_method)s, %(sport)s, %(program)s,
     %(started_at)s, %(ended_at)s, %(summary)s, %(raw)s, %(natural_key)s)
ON CONFLICT (source, activity_uid) DO UPDATE SET
    device           = EXCLUDED.device,
    recording_method = EXCLUDED.recording_method,
    sport            = EXCLUDED.sport,
    program          = EXCLUDED.program,
    started_at       = EXCLUDED.started_at,
    ended_at         = EXCLUDED.ended_at,
    summary          = EXCLUDED.summary,
    raw              = EXCLUDED.raw,
    ingested_at      = now()
"""


def _activity_header_params(row: dict) -> dict:
    """Build params for _ACTIVITY_HEADER_SQL from an activity row.

    Tolerates rows lacking a ``program`` key (cardio callers omit it) by
    defaulting to NULL, and wraps ``summary``/``raw`` with Jsonb only when the
    value is not None.
    """
    summary = row.get("summary")
    return {
        **row,
        "program": row.get("program"),
        "summary": Jsonb(summary) if summary is not None else None,
        "raw": Jsonb(row["raw"]),
    }


def upsert_samples(conn: Connection, rows: Iterable[dict]) -> int:
    # The ON CONFLICT ... DO UPDATE below can touch rows inside already-compressed
    # chunks, which requires TimescaleDB >= 2.16 (the project ships 2.23.1 via
    # nixos-25.11). This is what lets re-pulls of data older than the 14-day
    # compression policy succeed instead of erroring.
    sql = """
    INSERT INTO raw.samples
        (source, device, recording_method, metric, value, unit,
         valid_from, valid_to, natural_key, raw)
    VALUES
        (%(source)s, %(device)s, %(recording_method)s, %(metric)s, %(value)s, %(unit)s,
         %(valid_from)s, %(valid_to)s, %(natural_key)s, %(raw)s)
    ON CONFLICT (source, metric, natural_key, valid_from)
    DO UPDATE SET
        value            = EXCLUDED.value,
        unit             = EXCLUDED.unit,
        valid_to         = EXCLUDED.valid_to,
        device           = EXCLUDED.device,
        recording_method = EXCLUDED.recording_method,
        raw              = EXCLUDED.raw,
        ingested_at      = now()
    """
    params = [{**r, "raw": Jsonb(r["raw"])} for r in rows]
    if not params:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


def upsert_daily_metrics(conn: Connection, rows: Iterable[dict]) -> int:
    """Idempotent upsert of daily-summary scalars into raw.daily_metrics.

    Keyed on (source, metric, local_date): a re-pull of the same LOCAL day
    restates the row in place. Matches upsert_samples' shape (Jsonb-wrap raw,
    skip the round-trip on empty input, restatement handled by the ON UPDATE
    trigger)."""
    sql = """
    INSERT INTO raw.daily_metrics
        (source, device, recording_method, metric, value, unit,
         local_date, tz_offset_minutes, natural_key, raw)
    VALUES
        (%(source)s, %(device)s, %(recording_method)s, %(metric)s, %(value)s, %(unit)s,
         %(local_date)s, %(tz_offset_minutes)s, %(natural_key)s, %(raw)s)
    ON CONFLICT (source, metric, local_date)
    DO UPDATE SET
        value             = EXCLUDED.value,
        unit              = EXCLUDED.unit,
        tz_offset_minutes = EXCLUDED.tz_offset_minutes,
        device            = EXCLUDED.device,
        recording_method  = EXCLUDED.recording_method,
        raw               = EXCLUDED.raw,
        ingested_at       = now()
    """
    params = [{**r, "raw": Jsonb(r["raw"])} for r in rows]
    if not params:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


# Sleep session header, keyed on (source, session_uid). Sibling of the activity
# header upsert; sleep is a session, not a scalar sample.
_SLEEP_HEADER_SQL = """
INSERT INTO raw.sleep_sessions
    (source, session_uid, device, recording_method, started_at, ended_at,
     tz_offset_minutes, is_main_sleep, sleep_type, minutes_asleep, minutes_awake,
     minutes_in_sleep_period, minutes_to_fall_asleep, minutes_after_wakeup,
     efficiency, summary, raw, natural_key)
VALUES
    (%(source)s, %(session_uid)s, %(device)s, %(recording_method)s,
     %(started_at)s, %(ended_at)s, %(tz_offset_minutes)s, %(is_main_sleep)s,
     %(sleep_type)s, %(minutes_asleep)s, %(minutes_awake)s,
     %(minutes_in_sleep_period)s, %(minutes_to_fall_asleep)s,
     %(minutes_after_wakeup)s, %(efficiency)s,
     %(summary)s, %(raw)s, %(natural_key)s)
ON CONFLICT (source, session_uid) DO UPDATE SET
    device                  = EXCLUDED.device,
    recording_method        = EXCLUDED.recording_method,
    started_at              = EXCLUDED.started_at,
    ended_at                = EXCLUDED.ended_at,
    tz_offset_minutes       = EXCLUDED.tz_offset_minutes,
    is_main_sleep           = EXCLUDED.is_main_sleep,
    sleep_type              = EXCLUDED.sleep_type,
    minutes_asleep          = EXCLUDED.minutes_asleep,
    minutes_awake           = EXCLUDED.minutes_awake,
    minutes_in_sleep_period = EXCLUDED.minutes_in_sleep_period,
    minutes_to_fall_asleep  = EXCLUDED.minutes_to_fall_asleep,
    minutes_after_wakeup    = EXCLUDED.minutes_after_wakeup,
    efficiency              = EXCLUDED.efficiency,
    summary                 = EXCLUDED.summary,
    raw                     = EXCLUDED.raw,
    ingested_at             = now()
"""

_SLEEP_STAGE_SQL = """
INSERT INTO raw.sleep_stages (source, session_uid, stage, started_at, ended_at)
VALUES (%(source)s, %(session_uid)s, %(stage)s, %(started_at)s, %(ended_at)s)
ON CONFLICT (source, session_uid, started_at) DO UPDATE SET
    stage       = EXCLUDED.stage,
    ended_at    = EXCLUDED.ended_at,
    ingested_at = now()
"""


def upsert_sleep(conn: Connection, session_row: dict, stage_rows: list[dict]) -> None:
    """Upsert one sleep session: header into raw.sleep_sessions, stage segments
    into raw.sleep_stages, in a single transaction (same pattern as
    upsert_strength). ``summary`` is Jsonb-wrapped only when present."""
    summary = session_row.get("summary")
    header = {
        **session_row,
        "summary": Jsonb(summary) if summary is not None else None,
        "raw": Jsonb(session_row["raw"]),
    }
    with conn.cursor() as cur:
        cur.execute(_SLEEP_HEADER_SQL, header)
        if stage_rows:
            cur.executemany(_SLEEP_STAGE_SQL, stage_rows)
    conn.commit()


def upsert_activity(conn: Connection, row: dict) -> None:
    # Cardio path. Uses the shared activity-header upsert; cardio callers (e.g.
    # sources/intervals.py) build rows without a ``program`` key, which the
    # shared param builder defaults to NULL.
    p = _activity_header_params(row)
    with conn.cursor() as cur:
        cur.execute(_ACTIVITY_HEADER_SQL, p)
    conn.commit()


def upsert_activity_streams(conn: Connection, rows: Iterable[dict]) -> int:
    sql = """
    INSERT INTO raw.activity_streams (source, activity_uid, t, metric, value)
    VALUES (%(source)s, %(activity_uid)s, %(t)s, %(metric)s, %(value)s)
    ON CONFLICT (source, activity_uid, metric, t) DO UPDATE SET
        value = EXCLUDED.value, ingested_at = now()
    """
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_strength(
    conn: Connection,
    activity: dict,
    exercises: list[dict],
    sets: list[dict],
) -> None:
    """Upsert a strength workout: header into raw.activities (unified with
    cardio), exercises/sets into the strength child tables keyed by activity_uid.
    All three run in one transaction."""
    esql = """
    INSERT INTO raw.strength_exercises
        (source, activity_uid, exercise_uid, exercise_name, exercise_idx, is_unilateral, raw)
    VALUES
        (%(source)s, %(activity_uid)s, %(exercise_uid)s, %(exercise_name)s,
         %(exercise_idx)s, %(is_unilateral)s, %(raw)s)
    ON CONFLICT (source, activity_uid, exercise_uid) DO UPDATE SET
        exercise_name = EXCLUDED.exercise_name,
        exercise_idx  = EXCLUDED.exercise_idx,
        is_unilateral = EXCLUDED.is_unilateral,
        raw           = EXCLUDED.raw,
        ingested_at   = now()
    """
    ssql = """
    INSERT INTO raw.strength_sets
        (source, activity_uid, exercise_uid, set_index, completed_at,
         weight_kg, reps, left_reps, rpe, is_warmup, raw)
    VALUES
        (%(source)s, %(activity_uid)s, %(exercise_uid)s, %(set_index)s, %(completed_at)s,
         %(weight_kg)s, %(reps)s, %(left_reps)s, %(rpe)s, %(is_warmup)s, %(raw)s)
    ON CONFLICT (source, activity_uid, exercise_uid, set_index) DO UPDATE SET
        completed_at = EXCLUDED.completed_at,
        weight_kg    = EXCLUDED.weight_kg,
        reps         = EXCLUDED.reps,
        left_reps    = EXCLUDED.left_reps,
        rpe          = EXCLUDED.rpe,
        is_warmup    = EXCLUDED.is_warmup,
        raw          = EXCLUDED.raw,
        ingested_at  = now()
    """
    a = _activity_header_params(activity)
    with conn.cursor() as cur:
        cur.execute(_ACTIVITY_HEADER_SQL, a)
        if exercises:
            cur.executemany(esql, [{**e, "raw": Jsonb(e["raw"])} for e in exercises])
        if sets:
            cur.executemany(ssql, [{**s, "raw": Jsonb(s["raw"])} for s in sets])
    conn.commit()
