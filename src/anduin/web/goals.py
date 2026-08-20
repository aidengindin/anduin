"""Weight-goal phases: the one write path in the web UI.

``queries.py`` is read-only by design and stays that way; everything that
INSERTs lives here. A goal is a *phase* — an append-only row in
``identity.goals`` — and the phase in effect on a date is the latest row whose
``started_on`` is on or before it. Saving stamps ``current_date``, so correcting
a typo the same day rewrites that row rather than accumulating junk.

See docs/plans/2026-08-19-weight-goal-tracking-design.md.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

# 'none' is a tombstone: it ends a phase without starting a targeted one, so
# "done bulking, not yet cutting" is expressible. Absence of any row means no
# goal was ever set.
KINDS = ("bulk", "cut", "maintain", "none")
TARGETED_KINDS = ("bulk", "cut")

# A rate this large is a typo, not an intention.
MAX_TARGET_LB_PER_WEEK = 3.0


class GoalError(ValueError):
    """Invalid goal input. The message is shown to the user inline."""


def parse_goal(kind: str, target: str | float | None) -> tuple[str, float | None]:
    """Validate submitted form values into ``(kind, signed_target)``.

    The form collects a positive magnitude; the sign comes from the kind, so
    every comparison downstream is plain arithmetic with no sign-juggling.
    Untargeted kinds discard whatever the number field happened to hold.
    """
    if kind not in KINDS:
        raise GoalError(f"unknown goal kind: {kind}")
    if kind not in TARGETED_KINDS:
        return kind, None
    if target is None or str(target).strip() == "":
        raise GoalError(f"a {kind} needs a weekly target")
    try:
        magnitude = float(target)
    except ValueError:
        raise GoalError("weekly target must be a number") from None
    if not 0 < magnitude <= MAX_TARGET_LB_PER_WEEK:
        raise GoalError(f"weekly target must be between 0 and {MAX_TARGET_LB_PER_WEEK} lb")
    return kind, magnitude if kind == "bulk" else -magnitude


def current_goal(conn: Connection, user_id: int) -> dict[str, Any] | None:
    """The phase in effect today, or None if one was never set."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT kind, target_lb_per_week, started_on
            FROM identity.goals
            WHERE user_id = %(user_id)s AND started_on <= current_date
            ORDER BY started_on DESC
            LIMIT 1
        """, {"user_id": user_id})
        row = cur.fetchone()
    if row is None:
        return None
    target = row["target_lb_per_week"]
    return {
        "kind": row["kind"],
        "target": float(target) if target is not None else None,
        "started_on": row["started_on"],
    }


def set_goal(conn: Connection, user_id: int, kind: str, target: float | None) -> None:
    """Start a phase today. Re-saving the same day corrects that row."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO identity.goals (user_id, started_on, kind, target_lb_per_week)
            VALUES (%(user_id)s, current_date, %(kind)s, %(target)s)
            ON CONFLICT (user_id, started_on) DO UPDATE
                SET kind = EXCLUDED.kind,
                    target_lb_per_week = EXCLUDED.target_lb_per_week
        """, {"user_id": user_id, "kind": kind, "target": target})
