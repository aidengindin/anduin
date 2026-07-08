from __future__ import annotations

from datetime import date, datetime, timezone

from anduin.sources.google_health import _emit_companion, _emit_intraday


# --- BUG 1: intraday base-key selection is order-dependent ---


def test_emit_intraday_base_key_when_intraday_key_iterates_first():
    # Payload whose keys are ordered intraday-first. The base-total key
    # ('activities-heart') and the intraday key ('activities-heart-intraday')
    # both start with 'activities-'. Picking the first match naively would
    # select the intraday key as the base and then look for a (nonexistent)
    # 'activities-heart-intraday-intraday' -> drop the whole day's dataset.
    payload = {
        "activities-heart-intraday": {
            "dataset": [{"time": "00:00:00", "value": 60}]
        },
        "activities-heart": [{"value": 1}],
    }
    rows = _emit_intraday("heart_rate", "1sec", date(2026, 1, 1), payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["value"] == 60.0
    assert r["metric"] == "heart_rate"
    assert r["valid_from"] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_emit_intraday_normal_order_still_works():
    payload = {
        "activities-steps": [{"value": 100}],
        "activities-steps-intraday": {
            "dataset": [
                {"time": "00:01:00", "value": 5},
                {"time": "00:02:00", "value": 7},
            ]
        },
    }
    rows = _emit_intraday("steps", "1min", date(2026, 1, 1), payload)
    assert [r["value"] for r in rows] == [5.0, 7.0]


def test_emit_intraday_empty_dataset():
    payload = {
        "activities-heart": [{"value": 1}],
        "activities-heart-intraday": {"dataset": []},
    }
    assert _emit_intraday("heart_rate", "1sec", date(2026, 1, 1), payload) == []


# --- BUG 2: companion items lacking timestamps collapse via natural_key ---


def test_emit_companion_untimestamped_items_get_distinct_natural_keys():
    # Two spo2 readings on the same day, neither carrying dateTime/minute.
    # Both fall back to the date -> identical valid_from. With a date-only
    # natural_key they collapse to a single upsert row. They must instead get
    # DISTINCT natural_keys so both readings survive.
    payload = {"spo2": [{"value": 97}, {"value": 95}]}
    rows = _emit_companion("sleep_spo2", date(2026, 1, 1), payload)
    assert len(rows) == 2
    keys = {r["natural_key"] for r in rows}
    assert len(keys) == 2, f"natural_keys collapsed: {keys}"
    assert {r["value"] for r in rows} == {97.0, 95.0}


def test_emit_companion_preserves_timestamped_natural_key():
    # Items that DO carry a timestamp keep the timestamp-based natural_key.
    payload = {
        "hrv": [
            {"dateTime": "2026-01-01T01:00:00", "value": {"dailyRmssd": 42}},
        ]
    }
    rows = _emit_companion("sleep_hrv", date(2026, 1, 1), payload)
    assert len(rows) == 1
    dt = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert rows[0]["valid_from"] == dt
    assert rows[0]["natural_key"] == f"sleep_hrv|{dt.isoformat()}"


def test_emit_companion_two_timestamped_items_distinct():
    payload = {
        "spo2": [
            {"dateTime": "2026-01-01T01:00:00", "value": 97},
            {"dateTime": "2026-01-01T02:00:00", "value": 95},
        ]
    }
    rows = _emit_companion("sleep_spo2", date(2026, 1, 1), payload)
    assert len({r["natural_key"] for r in rows}) == 2
