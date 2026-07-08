from __future__ import annotations

from datetime import datetime, timezone

from anduin.sources.intervals import _activity_window, _emit_streams


def test_emit_streams_walks_per_second():
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "streams": [
            {"type": "heartrate", "data": [60, 61, None, 63]},
            {"type": "watts",     "data": [100, 110, 120, 130]},
        ]
    }
    rows = _emit_streams("act-1", started, payload)
    # 3 valid HR + 4 watts = 7
    assert len(rows) == 7
    hr = [r for r in rows if r["metric"] == "heartrate"]
    assert [r["value"] for r in hr] == [60.0, 61.0, 63.0]
    assert hr[0]["t"] == started
    assert hr[1]["t"].second == 1
    assert hr[2]["t"].second == 3  # skipped the None at index 2
    assert all(r["activity_uid"] == "act-1" for r in rows)
    assert all(r["source"] == "intervals" for r in rows)


def test_emit_streams_handles_empty():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _emit_streams("a", started, None) == []
    assert _emit_streams("a", started, {"streams": []}) == []
    assert _emit_streams("a", started, []) == []


def test_activity_window_prefers_utc_over_local():
    # start_date carries a 'Z' (true UTC); start_date_local is naive wall-clock.
    # A 07:00 local activity is 14:00 UTC. We must store the true UTC instant,
    # not mislabel the naive local time as UTC.
    act = {
        "start_date_local": "2026-07-08T07:00:00",
        "start_date": "2026-07-08T14:00:00Z",
        "end_date_local": "2026-07-08T08:00:00",
        "end_date": "2026-07-08T15:00:00Z",
    }
    started_at, ended_at = _activity_window(act)
    assert started_at == datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc)
    assert ended_at == datetime(2026, 7, 8, 15, 0, 0, tzinfo=timezone.utc)


def test_activity_window_falls_back_to_local_labeled_utc():
    # Only the *_local fields are present -> use them, labeled UTC as last resort.
    act = {
        "start_date_local": "2026-07-08T07:00:00",
        "end_date_local": "2026-07-08T08:00:00",
    }
    started_at, ended_at = _activity_window(act)
    assert started_at == datetime(2026, 7, 8, 7, 0, 0, tzinfo=timezone.utc)
    assert ended_at == datetime(2026, 7, 8, 8, 0, 0, tzinfo=timezone.utc)


def test_activity_window_computes_end_from_elapsed_time():
    # No end field -> derive from elapsed_time (falling back to moving_time).
    act = {
        "start_date": "2026-07-08T14:00:00Z",
        "elapsed_time": 3600,
    }
    started_at, ended_at = _activity_window(act)
    assert started_at == datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc)
    assert ended_at == datetime(2026, 7, 8, 15, 0, 0, tzinfo=timezone.utc)
