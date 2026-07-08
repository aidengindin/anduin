"""Parser for Liftosaur's Liftohistory text format.

The live Liftosaur API (`GET /api/v1/history`, Bearer auth) returns workout
records as human-readable text, e.g.:

    2026-05-27 10:17:19 +00:00 / program: "Ironman Maintenance" / dayName: "Day 1"
      / week: 1 / dayInWeek: 1 / duration: 2276s / exercises: {
      Romanian Deadlift, Barbell / 2x8 165lb / warmup: 1x10 125lb / target: 2x11 165lb 120s
      Bulgarian Split Squat / 2x6|6 50lb / warmup: 1x12|12 22.5lb / target: 2x9 50lb 90s
    }

Set notation: `NxR[|L][+] Wunit [@RPE[+]] [Ts]` — N sets of R reps (R|L for
unilateral, right|left), optional `+` AMRAP, weight+unit, optional `@RPE` (a
trailing `+` marks a logged RPE), optional rest timer. An exercise line is
`Name[, Equipment] / <completed> / warmup: <sets> / target: <sets>`; `target:`
is prescribed (not performed) and is skipped.

This module is pure (no I/O) so it can be unit-tested against real records.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

LBS_PER_KG = 2.2046226218

_SET_GROUP_RE = re.compile(
    r"^(\d+)x"                 # N sets
    r"(\d+)"                   # reps (primary / right)
    r"(?:\|(\d+))?"            # optional |left
    r"(\+)?"                   # optional AMRAP marker
    r"\s+(-?[\d.]+)(lb|kg)"    # weight + unit (negative = assisted)
    r"(?:\s+@([\d.]+)(\+)?)?"  # optional @RPE[+]
    r"(?:\s+(\d+)s)?"          # optional rest timer (target sections)
    r"$"
)

_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|\s*[+-]\d{2}:\d{2})?)"
)
_PROGRAM_RE = re.compile(r'program: "([^"]*)"')
_DAYNAME_RE = re.compile(r'dayName: "([^"]*)"')
_DURATION_RE = re.compile(r"duration: (\d+)s")
_EXERCISES_RE = re.compile(r"exercises: \{([\s\S]*)\}")


def _to_kg(value: float, unit: str) -> float:
    return value if unit == "kg" else value / LBS_PER_KG


def _parse_ts(s: str) -> datetime:
    s = s.strip()
    s = re.sub(r"\s+([+-]\d{2}:\d{2})$", r"\1", s)  # drop space before offset
    s = s.replace(" ", "T", 1)                      # date/time separator
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_set_group(group: str) -> list[dict]:
    """Expand one set group like `2x6|6 50lb @7` into a list of set dicts.

    Returns [] for anything that doesn't match (e.g. a target rep-range
    `2x6-15`), so callers can pass raw tokens without pre-filtering.
    """
    m = _SET_GROUP_RE.match(group.strip())
    if not m:
        return []
    n, reps, left, amrap, weight, unit, rpe, _rpe_logged, _timer = m.groups()
    one = {
        "reps": int(reps),
        "left_reps": int(left) if left is not None else None,
        "weight": float(weight),
        "unit": unit,
        "rpe": float(rpe) if rpe is not None else None,
        "is_amrap": amrap is not None,
    }
    return [dict(one) for _ in range(int(n))]


def parse_exercise_line(line: str) -> dict:
    """Parse one exercise line into {name, is_unilateral, sets}. Working and
    warmup sets are kept (with is_warmup); target (prescribed) sets are dropped."""
    parts = [p.strip() for p in line.split(" / ")]
    name = parts[0]
    sets: list[dict] = []
    is_unilateral = False
    for token in parts[1:]:
        if token.startswith("target:"):
            continue
        if token.startswith("warmup:"):
            segment, is_warmup = token[len("warmup:"):].strip(), True
        else:
            segment, is_warmup = token, False
        for group in segment.split(","):
            for s in parse_set_group(group):
                s["is_warmup"] = is_warmup
                if s["left_reps"] is not None:
                    is_unilateral = True
                sets.append(s)
    return {"name": name, "is_unilateral": is_unilateral, "sets": sets}


def parse_workout(text: str) -> dict:
    """Parse a full Liftohistory record into header fields + parsed exercises."""
    ts_m = _TS_RE.match(text.strip())
    started_at = _parse_ts(ts_m.group(1)) if ts_m else None
    program_m = _PROGRAM_RE.search(text)
    dayname_m = _DAYNAME_RE.search(text)
    duration_m = _DURATION_RE.search(text)
    exercises: list[dict] = []
    block_m = _EXERCISES_RE.search(text)
    if block_m:
        for raw_line in block_m.group(1).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//") or line in ("{", "}"):
                continue
            exercises.append(parse_exercise_line(line))
    return {
        "started_at": started_at,
        "duration_s": int(duration_m.group(1)) if duration_m else None,
        "program": program_m.group(1) if program_m else None,
        "day_name": dayname_m.group(1) if dayname_m else None,
        "exercises": exercises,
    }


def build_strength_rows(record_id: int, text: str) -> dict:
    """Map a Liftosaur record to raw.activities + strength_{exercises,sets} rows.

    Returns {"activity": dict, "exercises": [dict], "sets": [dict]} ready for
    upsert_strength(). Sets have no per-set timestamp in the text format, so
    completed_at is stamped with the workout start.
    """
    wk = parse_workout(text)
    activity_uid = str(record_id)
    started_at = wk["started_at"]
    duration = wk["duration_s"]
    ended_at = (
        started_at + timedelta(seconds=duration)
        if started_at is not None and duration is not None
        else None
    )

    activity = {
        "source": "liftosaur",
        "activity_uid": activity_uid,
        "device": None,
        "recording_method": "liftosaur",
        "sport": "strength",
        "program": wk["program"],
        "started_at": started_at,
        "ended_at": ended_at,
        "summary": None,
        "raw": {"id": record_id, "text": text},
        "natural_key": activity_uid,
    }

    exercises: list[dict] = []
    sets: list[dict] = []
    for idx, ex in enumerate(wk["exercises"]):
        ex_uid = f"{activity_uid}/{idx}"
        exercises.append({
            "source": "liftosaur",
            "activity_uid": activity_uid,
            "exercise_uid": ex_uid,
            "exercise_name": ex["name"],
            "exercise_idx": idx,
            "is_unilateral": ex["is_unilateral"],
            "raw": ex,
        })
        for si, s in enumerate(ex["sets"]):
            sets.append({
                "source": "liftosaur",
                "activity_uid": activity_uid,
                "exercise_uid": ex_uid,
                "set_index": si,
                "completed_at": started_at,
                "weight_kg": _to_kg(s["weight"], s["unit"]),
                "reps": s["reps"],
                "left_reps": s["left_reps"],
                "rpe": s["rpe"],
                "is_warmup": s["is_warmup"],
                "raw": s,
            })

    return {"activity": activity, "exercises": exercises, "sets": sets}
