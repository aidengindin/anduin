"""Tests for the Liftohistory text-format parser + DB row mapper.

Fixtures are real records pulled from /api/v1/history (symmetric unilateral like
`2x6|6`); asymmetric cases (`3x8|7`) are synthetic per the format reference.
"""

from __future__ import annotations

from anduin.sources.liftohistory import (
    build_strength_rows,
    parse_set_group,
    parse_exercise_line,
    parse_workout,
)

# A real record (2026-05-27), lightly trimmed.
REAL_TEXT = (
    '2026-05-27 10:17:19 +00:00 / program: "Ironman Maintenance" / dayName: "Day 1" '
    "/ week: 1 / dayInWeek: 1 / duration: 2276s / exercises: {\n"
    "  Romanian Deadlift, Barbell / 2x8 165lb / warmup: 1x10 125lb / target: 2x11 165lb 120s\n"
    "  Bulgarian Split Squat / 2x6|6 50lb / warmup: 1x12|12 22.5lb / target: 2x9 50lb 90s\n"
    "  Pull Up / 1x8 0lb, 1x7 0lb / target: 2x6-15 90s\n"
    "  Pallof Press / 2x10|10 45lb / target: 2x11-12 45lb 60s\n"
    "}"
)


# --- parse_set_group ---------------------------------------------------------

def test_set_group_bilateral_with_rpe():
    sets = parse_set_group("3x8 185lb @7")
    assert len(sets) == 3
    assert all(s == {"reps": 8, "left_reps": None, "weight": 185.0,
                     "unit": "lb", "rpe": 7.0, "is_amrap": False} for s in sets)


def test_set_group_unilateral_symmetric():
    sets = parse_set_group("2x6|6 50lb")
    assert len(sets) == 2
    assert sets[0]["reps"] == 6 and sets[0]["left_reps"] == 6


def test_set_group_unilateral_asymmetric():
    (s,) = parse_set_group("1x8|7 0lb")
    assert s["reps"] == 8 and s["left_reps"] == 7 and s["weight"] == 0.0


def test_set_group_amrap():
    (s,) = parse_set_group("1x5+ 185lb")
    assert s["reps"] == 5 and s["is_amrap"] is True


def test_set_group_logged_rpe():
    (s,) = parse_set_group("3x8 185lb @8+")[:1]
    assert s["rpe"] == 8.0


def test_set_group_decimal_kg():
    (s,) = parse_set_group("1x12 22.5kg")
    assert s["weight"] == 22.5 and s["unit"] == "kg"


def test_set_group_negative_weight_assisted():
    # Assisted exercises log negative weight (e.g. -10lb of assistance).
    sets = parse_set_group("2x7 -10lb")
    assert len(sets) == 2
    assert sets[0]["reps"] == 7 and sets[0]["weight"] == -10.0


# --- parse_exercise_line -----------------------------------------------------

def test_exercise_line_working_and_warmup_skip_target():
    ex = parse_exercise_line(
        "Romanian Deadlift, Barbell / 2x8 165lb / warmup: 1x10 125lb / target: 2x11 165lb 120s"
    )
    assert ex["name"] == "Romanian Deadlift, Barbell"
    assert ex["is_unilateral"] is False
    working = [s for s in ex["sets"] if not s["is_warmup"]]
    warmup = [s for s in ex["sets"] if s["is_warmup"]]
    assert len(working) == 2 and working[0]["reps"] == 8
    assert len(warmup) == 1 and warmup[0]["reps"] == 10
    # target set (prescribed, not performed) is excluded entirely
    assert len(ex["sets"]) == 3


def test_exercise_line_unilateral_flag():
    ex = parse_exercise_line("Bulgarian Split Squat / 2x6|6 50lb / target: 2x9 50lb 90s")
    assert ex["is_unilateral"] is True


def test_exercise_line_comma_separated_groups():
    ex = parse_exercise_line("Pull Up / 1x8 0lb, 1x7 0lb / target: 2x6-15 90s")
    working = [s for s in ex["sets"] if not s["is_warmup"]]
    assert [s["reps"] for s in working] == [8, 7]


# --- parse_workout -----------------------------------------------------------

def test_parse_workout_header_and_exercises():
    wk = parse_workout(REAL_TEXT)
    assert wk["program"] == "Ironman Maintenance"
    assert wk["duration_s"] == 2276
    assert wk["started_at"].year == 2026 and wk["started_at"].month == 5
    assert [e["name"] for e in wk["exercises"]] == [
        "Romanian Deadlift, Barbell",
        "Bulgarian Split Squat",
        "Pull Up",
        "Pallof Press",
    ]


# --- build_strength_rows (DB mapper) -----------------------------------------

def test_build_rows_activity_header():
    rows = build_strength_rows(1779877039893, REAL_TEXT)
    a = rows["activity"]
    assert a["source"] == "liftosaur"
    assert a["activity_uid"] == "1779877039893"
    assert a["sport"] == "strength"
    assert a["program"] == "Ironman Maintenance"
    assert a["ended_at"] is not None  # started + duration


def test_build_rows_unilateral_set_weight_and_index():
    rows = build_strength_rows(1779877039893, REAL_TEXT)
    # Bulgarian Split Squat is exercise index 1, unilateral.
    ex = [e for e in rows["exercises"] if e["exercise_idx"] == 1][0]
    assert ex["is_unilateral"] is True
    bss_sets = [s for s in rows["sets"] if s["exercise_uid"] == ex["exercise_uid"]]
    working = [s for s in bss_sets if not s["is_warmup"]]
    assert len(working) == 2
    assert working[0]["reps"] == 6 and working[0]["left_reps"] == 6
    # 50 lb -> kg
    assert abs(working[0]["weight_kg"] - 50 / 2.2046226218) < 1e-6
    # set_index is unique within the exercise
    assert len({s["set_index"] for s in bss_sets}) == len(bss_sets)


def test_build_rows_bodyweight_zero_weight():
    rows = build_strength_rows(1779877039893, REAL_TEXT)
    pullup = [e for e in rows["exercises"] if e["exercise_name"] == "Pull Up"][0]
    sets = [s for s in rows["sets"] if s["exercise_uid"] == pullup["exercise_uid"]]
    assert all(s["weight_kg"] == 0.0 for s in sets)
