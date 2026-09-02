"""Headache journal: form parsing + persistence.

DB-free, same scripted fake cursor as ``test_web_queries``. The SQL itself is
exercised against the real database in the manual verification pass.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from anduin import journal
from tests.test_web_queries import FakeConn

NOW = datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc)   # 14:30 in New York
EDT = -240


class Form(dict):
    """Starlette FormData look-alike: ``get`` plus ``getlist``."""

    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]


def _parse(**fields):
    return journal.parse_checkin(Form(fields), now=NOW, server_offset_minutes=EDT)


# --- parse_checkin: intensity + symptoms -----------------------------------


def test_intensity_is_required():
    with pytest.raises(journal.JournalError):
        _parse()


@pytest.mark.parametrize("bad", ["-1", "11", "abc", ""])
def test_intensity_must_be_0_to_10(bad):
    with pytest.raises(journal.JournalError):
        _parse(intensity=bad)


def test_a_bare_zero_is_a_complete_checkin():
    c = _parse(intensity="0")
    assert c.intensity == 0
    assert c.nausea == 0 and c.light is False and c.noise is False
    assert c.qualities == [] and c.note is None
    assert c.source == "app"


def test_symptoms_ride_along_with_the_intensity():
    c = _parse(intensity="6", nausea="2", light="on", noise="on",
               qualities=["throbbing", "unilateral"], note=" left temple ")
    assert c.intensity == 6 and c.nausea == 2
    assert c.light is True and c.noise is True
    assert c.qualities == ["throbbing", "unilateral"]
    assert c.note == "left temple"


def test_unknown_quality_is_rejected():
    with pytest.raises(journal.JournalError):
        _parse(intensity="4", qualities=["stabby"])


@pytest.mark.parametrize("bad", ["-1", "4", "x"])
def test_nausea_must_be_0_to_3(bad):
    with pytest.raises(journal.JournalError):
        _parse(intensity="4", nausea=bad)


def test_overlong_note_is_rejected():
    with pytest.raises(journal.JournalError):
        _parse(intensity="4", note="x" * (journal.NOTE_MAX + 1))


def test_blank_note_is_stored_as_null():
    assert _parse(intensity="4", note="   ").note is None


def test_source_defaults_to_app_and_accepts_ntfy_only():
    assert _parse(intensity="0", source="ntfy").source == "ntfy"
    with pytest.raises(journal.JournalError):
        _parse(intensity="0", source="airtable")


# --- parse_checkin: time + civil date --------------------------------------


def test_default_time_is_now_with_the_browser_offset():
    c = _parse(intensity="3", tz_offset="-240")
    assert c.logged_at == NOW
    assert c.tz_offset_minutes == -240
    assert c.local_date == date(2026, 9, 1)


def test_server_offset_is_the_fallback_when_no_browser_offset_arrives():
    c = _parse(intensity="0", source="ntfy")
    assert c.tz_offset_minutes == EDT
    assert c.local_date == date(2026, 9, 1)


def test_local_date_follows_the_wearer_not_utc():
    # 23:30 in New York on Aug 31 is 03:30 UTC on Sep 1: the civil date is Aug 31.
    late = datetime(2026, 9, 1, 3, 30, tzinfo=timezone.utc)
    c = journal.parse_checkin(Form(intensity="2", tz_offset="-240"),
                              now=late, server_offset_minutes=0)
    assert c.local_date == date(2026, 8, 31)


def test_an_explicit_time_is_read_as_browser_local_wall_time():
    # datetime-local has no zone: "13:05" in New York is 17:05 UTC.
    c = _parse(intensity="7", at="2026-09-01T13:05", tz_offset="-240")
    assert c.logged_at == datetime(2026, 9, 1, 17, 5, tzinfo=timezone.utc)
    assert c.local_date == date(2026, 9, 1)


def test_a_future_time_is_rejected():
    with pytest.raises(journal.JournalError):
        _parse(intensity="7", at="2026-09-01T15:00", tz_offset="-240")  # 19:00Z > NOW


def test_a_time_older_than_the_backfill_window_is_rejected():
    with pytest.raises(journal.JournalError):
        _parse(intensity="7", at="2026-07-01T09:00", tz_offset="-240")


def test_garbage_time_or_offset_is_rejected():
    with pytest.raises(journal.JournalError):
        _parse(intensity="7", at="yesterday-ish")
    with pytest.raises(journal.JournalError):
        _parse(intensity="7", tz_offset="EST")


# --- parse_day_context -----------------------------------------------------


def test_day_context_only_returns_the_fields_submitted():
    assert journal.parse_day_context(Form(fluorescent="hours")) == {"fluorescent_exposure": "hours"}
    assert journal.parse_day_context(Form(peak="8")) == {"peak_intensity": 8}
    # The day note's field is "daynote": the check-in form has its own "note".
    assert journal.parse_day_context(Form(daynote=" office ")) == {"note": "office"}
    assert journal.parse_day_context(Form(daynote="")) == {"note": None}


def test_an_empty_peak_clears_the_override():
    assert journal.parse_day_context(Form(peak="")) == {"peak_intensity": None}


def test_intake_counts_are_parsed_together_and_empty_means_unknown():
    assert journal.parse_day_context(Form(coffee="2", alcohol="0")) == {
        "coffee_cups": 2, "alcohol_drinks": 0,
    }
    assert journal.parse_day_context(Form(coffee="", alcohol="1")) == {
        "coffee_cups": None, "alcohol_drinks": 1,
    }


@pytest.mark.parametrize("form", [{"coffee": "21"}, {"alcohol": "-1"}, {"coffee": "two"}])
def test_bad_intake_counts_are_rejected(form):
    with pytest.raises(journal.JournalError):
        journal.parse_day_context(Form(form))


@pytest.mark.parametrize("form", [{"fluorescent": "lots"}, {"peak": "11"}, {"peak": "x"}])
def test_bad_day_context_is_rejected(form):
    with pytest.raises(journal.JournalError):
        journal.parse_day_context(Form(form))


def test_nothing_to_update_is_rejected():
    with pytest.raises(journal.JournalError):
        journal.parse_day_context(Form())


# --- persistence -----------------------------------------------------------


def test_add_checkin_stamps_the_configured_user_and_returns_the_id():
    conn = FakeConn([{"id": 42}])
    c = _parse(intensity="5", qualities=["pressure"], tz_offset="-240")
    assert journal.add_checkin(conn, 7, c) == 42
    sql, params = conn._cursor.executed[0]
    assert "INSERT INTO journal.headache_checkins" in sql
    assert "ON CONFLICT (user_id, logged_at) DO NOTHING" in sql
    assert params["user_id"] == 7 and params["intensity"] == 5
    assert params["qualities"] == ["pressure"] and params["local_date"] == date(2026, 9, 1)


def test_add_checkin_double_tap_returns_none_instead_of_raising():
    conn = FakeConn([None])
    assert journal.add_checkin(conn, 1, _parse(intensity="0")) is None


def test_delete_checkin_is_scoped_to_the_user():
    conn = FakeConn([None])
    journal.delete_checkin(conn, 3, 99)
    sql, params = conn._cursor.executed[0]
    assert "DELETE FROM journal.headache_checkins" in sql
    assert params == {"user_id": 3, "id": 99}


def test_latest_checkin_at_is_none_when_nothing_was_ever_logged():
    assert journal.latest_checkin_at(FakeConn([None]), 1) is None


def test_latest_checkin_at_returns_the_timestamp():
    conn = FakeConn([{"logged_at": NOW}])
    assert journal.latest_checkin_at(conn, 1) == NOW


def test_set_day_context_upserts_only_the_given_columns():
    conn = FakeConn([None])
    journal.set_day_context(conn, 1, date(2026, 9, 1), {"peak_intensity": 8})
    sql, params = conn._cursor.executed[0]
    assert "INSERT INTO journal.headache_days" in sql
    assert "ON CONFLICT (user_id, local_date) DO UPDATE" in sql
    assert "peak_intensity = EXCLUDED.peak_intensity" in sql
    assert "fluorescent_exposure = EXCLUDED" not in sql
    assert "updated_at = now()" in sql
    assert params == {"user_id": 1, "local_date": date(2026, 9, 1), "peak_intensity": 8}


def test_set_day_context_refuses_unknown_columns():
    with pytest.raises(ValueError):
        journal.set_day_context(FakeConn([None]), 1, date(2026, 9, 1), {"user_id": 2})


# --- timeline strip --------------------------------------------------------


def test_timeline_points_place_checkins_by_time_of_day():
    rows = [
        {"logged_at": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc), "tz_offset_minutes": -240, "intensity": 2},   # 09:00 local
        {"logged_at": datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc), "tz_offset_minutes": -240, "intensity": 7},   # 15:00 local
    ]
    pts = journal.timeline_points(rows, width=240, height=40)
    xs = [float(p.split(",")[0]) for p in pts.split()]
    ys = [float(p.split(",")[1]) for p in pts.split()]
    assert xs[0] == pytest.approx(240 * 9 / 24) and xs[1] == pytest.approx(240 * 15 / 24)
    assert ys[1] < ys[0]            # higher intensity is higher on the strip
    assert journal.timeline_points([], width=240, height=40) == ""
