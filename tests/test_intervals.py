"""Emitter tests for the intervals.icu extractor."""

from __future__ import annotations

from datetime import date

from anduin.config import IntervalsConfig
from anduin.sources.intervals import _is_skipped, _wellness_rows


def test_wellness_rows_emits_ctl_and_atl():
    records = [
        {"id": "2026-07-10", "ctl": 76.0, "atl": 80.5, "rampRate": -2.3},
        {"id": "2026-07-11", "ctl": 75.1, "atl": 78.2},
    ]
    rows = _wellness_rows(records)
    by = {(r["metric"], r["local_date"]): r for r in rows}
    assert by[("ctl", date(2026, 7, 10))]["value"] == 76.0
    assert by[("atl", date(2026, 7, 10))]["value"] == 80.5
    assert by[("ctl", date(2026, 7, 11))]["value"] == 75.1
    r = by[("ctl", date(2026, 7, 10))]
    assert r["source"] == "intervals"
    assert r["natural_key"] == "ctl|2026-07-10"
    assert r["local_date"] == date(2026, 7, 10)


def test_wellness_rows_skips_missing_date_or_values():
    records = [
        {"ctl": 50.0},                       # no id -> skipped entirely
        {"id": "2026-07-12", "atl": 60.0},   # ctl missing -> only atl row
        {"id": "2026-07-13", "ctl": None, "atl": None},  # both null -> nothing
    ]
    rows = _wellness_rows(records)
    assert [(r["metric"], r["local_date"]) for r in rows] == [("atl", date(2026, 7, 12))]


def test_is_skipped_matches_liftosaur_mirror_by_default():
    patterns = IntervalsConfig().skip_name_contains
    # Real shape of the mirrored activities: "Liftosaur: Week 2 - Day 2 (A)".
    assert _is_skipped({"name": "Liftosaur: Week 2 - Day 2 (A)"}, patterns)
    assert _is_skipped({"name": "liftosaur: week 1 - day 1"}, patterns)  # case-insensitive


def test_is_skipped_leaves_real_activities_alone():
    patterns = IntervalsConfig().skip_name_contains
    assert not _is_skipped({"name": "Morning Ride"}, patterns)
    assert not _is_skipped({"name": None}, patterns)
    assert not _is_skipped({}, patterns)
    assert not _is_skipped({"name": "Liftosaur: Week 1"}, [])  # opt-out via config
