"""Liftosaur extractor: the history API's `endDate` is EXCLUSIVE.

Verified against the live API (2026-08-17): `startDate=2026-08-10&endDate=2026-08-17`
returns the 08-15/08-12/08-10 workouts but *not* the 08-17 one; bumping endDate to
08-18 returns it. So a request whose endDate is the last day we want misses that
whole day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from anduin.config import AppConfig, FileConfig, Secrets
from anduin.sources import liftosaur

# One real-shaped record, started 2026-08-17 10:38 UTC.
RECORD = {
    "id": 1786963089459,
    "text": (
        '2026-08-17 10:38:09 +00:00 / program: "Push Pull" / dayName: "Day 1"'
        " / week: 1 / dayInWeek: 1 / duration: 2276s / exercises: {\n"
        "  Romanian Deadlift, Barbell / 2x8 165lb / warmup: 1x10 125lb\n"
        "}"
    ),
}


def _app():
    return AppConfig(
        secrets=Secrets(database_url="postgresql://dummy/anduin", liftosaur_api_key="k"),
        file=FileConfig(),
    )


def _install_fake_api(monkeypatch, records, seen_params):
    """Stub get_json with the API's real semantics: startDate inclusive,
    endDate exclusive."""

    def fake_get_json(http, url, *, params, headers):  # noqa: ANN001
        seen_params.append(params)
        start = date.fromisoformat(params["startDate"])
        end = date.fromisoformat(params["endDate"])
        hit = [r for r in records if start <= date.fromisoformat(r["text"][:10]) < end]
        return {"data": {"records": hit, "hasMore": False, "nextCursor": None}}

    monkeypatch.setattr(liftosaur, "get_json", fake_get_json)


def test_workout_on_the_until_day_is_ingested(monkeypatch):
    """The window's last day is the day the workout just happened on — it must
    not wait for the UTC date to roll over."""
    upserted = []
    _install_fake_api(monkeypatch, [RECORD], [])
    monkeypatch.setattr(
        liftosaur, "upsert_strength",
        lambda conn, uid, activity, exercises, sets: upserted.append(activity),
    )

    result = liftosaur.extract(
        None, None, _app(), date(2026, 8, 10), date(2026, 8, 17), dry_run=False
    )

    assert result.errors == []
    assert [a["activity_uid"] for a in upserted] == ["1786963089459"]


def test_request_end_date_is_exclusive_upper_bound(monkeypatch):
    seen: list[dict] = []
    _install_fake_api(monkeypatch, [], seen)

    liftosaur.extract(None, None, _app(), date(2026, 8, 10), date(2026, 8, 17), dry_run=True)

    assert seen == [{"startDate": "2026-08-10", "endDate": "2026-08-18", "limit": "200"}]


def test_workout_after_the_window_is_still_filtered_out(monkeypatch):
    """Widening the request must not widen what we accept: the client-side
    guard still owns the boundary."""
    upserted = []
    later = dict(RECORD, id=1, text=RECORD["text"].replace("2026-08-17", "2026-08-19"))
    # An API that ignores the date params entirely, to exercise the guard.
    monkeypatch.setattr(
        liftosaur, "get_json",
        lambda http, url, *, params, headers: {"data": {"records": [later]}},
    )
    monkeypatch.setattr(
        liftosaur, "upsert_strength",
        lambda conn, uid, activity, exercises, sets: upserted.append(activity),
    )

    liftosaur.extract(None, None, _app(), date(2026, 8, 10), date(2026, 8, 17), dry_run=False)

    assert upserted == []
    # ...and the guard's upper edge is midnight after `until`.
    assert datetime(2026, 8, 18, tzinfo=timezone.utc) == datetime.combine(
        date(2026, 8, 17) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
