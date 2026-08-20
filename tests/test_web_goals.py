"""Goal phase parsing + persistence for the weight-goal editor.

DB-free: the same scripted fake cursor pattern as ``test_web_queries``. The SQL
itself is validated against the real database by the manual verification pass.
"""

from __future__ import annotations

from datetime import date

import pytest

from anduin.web import goals
from tests.test_web_queries import FakeConn


# --- parse_goal ------------------------------------------------------------


def test_bulk_target_is_stored_positive():
    assert goals.parse_goal("bulk", "0.4") == ("bulk", 0.4)


def test_cut_target_is_negated():
    assert goals.parse_goal("cut", "1.0") == ("cut", -1.0)


def test_maintain_discards_any_submitted_target():
    assert goals.parse_goal("maintain", "0.4") == ("maintain", None)


def test_none_kind_is_a_tombstone_with_no_target():
    assert goals.parse_goal("none", None) == ("none", None)


def test_unknown_kind_is_rejected():
    with pytest.raises(goals.GoalError):
        goals.parse_goal("recomp", "0.4")


@pytest.mark.parametrize("target", [None, "", "abc"])
def test_bulk_requires_a_numeric_target(target):
    with pytest.raises(goals.GoalError):
        goals.parse_goal("bulk", target)


@pytest.mark.parametrize("target", ["0", "-0.4", "3.5"])
def test_target_must_be_a_positive_magnitude_within_bounds(target):
    with pytest.raises(goals.GoalError):
        goals.parse_goal("bulk", target)


def test_target_at_the_upper_bound_is_accepted():
    assert goals.parse_goal("bulk", "3") == ("bulk", 3.0)


# --- current_goal ----------------------------------------------------------


def test_current_goal_shapes_the_row():
    conn = FakeConn([{"kind": "bulk", "target_lb_per_week": 0.4,
                      "started_on": date(2026, 8, 1)}])
    assert goals.current_goal(conn, 1) == {
        "kind": "bulk", "target": 0.4, "started_on": date(2026, 8, 1),
    }


def test_current_goal_is_none_when_no_phase_was_ever_set():
    assert goals.current_goal(FakeConn([None]), 1) is None


def test_a_none_tombstone_reads_back_as_no_active_target():
    conn = FakeConn([{"kind": "none", "target_lb_per_week": None,
                      "started_on": date(2026, 8, 1)}])
    goal = goals.current_goal(conn, 1)
    assert goal["kind"] == "none" and goal["target"] is None


# --- set_goal --------------------------------------------------------------


def test_set_goal_upserts_on_the_same_day():
    conn = FakeConn([None])
    goals.set_goal(conn, 1, "bulk", 0.4)
    sql, params = conn._cursor.executed[0]
    assert "ON CONFLICT" in sql.upper()
    assert params["user_id"] == 1
    assert params["kind"] == "bulk"
    assert params["target"] == 0.4


def test_set_goal_stamps_the_configured_user():
    conn = FakeConn([None])
    goals.set_goal(conn, 7, "maintain", None)
    _, params = conn._cursor.executed[0]
    assert params["user_id"] == 7 and params["target"] is None
