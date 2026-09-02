"""ntfy headache reminders: the skip window, the payload, the send."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import httpx
import respx

from anduin import cli, remind
from anduin.config import AppConfig, FileConfig, HeadacheConfig, Secrets

NOW = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> HeadacheConfig:
    return HeadacheConfig(**{"app_url": "https://anduin.tail.ts.net", **kw})


def _app(*, topic="t0p1c", token="", app_url="https://anduin.tail.ts.net") -> AppConfig:
    return AppConfig(
        secrets=Secrets(database_url="postgresql://dummy/anduin", ntfy_topic=topic, ntfy_token=token),
        file=FileConfig(headache=HeadacheConfig(app_url=app_url)),
    )


# --- should_remind -----------------------------------------------------------


def test_reminds_when_nothing_was_ever_logged():
    assert remind.should_remind(None, NOW, 120) is True


def test_skips_when_a_checkin_landed_inside_the_window():
    assert remind.should_remind(NOW - timedelta(minutes=30), NOW, 120) is False


def test_reminds_once_the_window_has_passed():
    assert remind.should_remind(NOW - timedelta(hours=3), NOW, 120) is True


def test_the_window_edge_still_skips():
    assert remind.should_remind(NOW - timedelta(minutes=120), NOW, 120) is False


# --- payload -------------------------------------------------------------------


def test_payload_offers_no_headache_and_open_the_log():
    p = remind.build_notification(_cfg(), "t0p1c")
    assert p["topic"] == "t0p1c"
    assert p["click"] == "https://anduin.tail.ts.net/log"
    # iOS caps action buttons at three; two is all a check-in needs.
    assert len(p["actions"]) == 2
    no, log = p["actions"]
    assert no["action"] == "http" and no["method"] == "POST"
    assert no["url"] == "https://anduin.tail.ts.net/api/log/headache"
    assert no["body"] == "intensity=0&source=ntfy"
    assert no["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert no["clear"] is True
    assert log["action"] == "view" and log["url"] == "https://anduin.tail.ts.net/log"


def test_payload_tolerates_a_trailing_slash_on_app_url():
    p = remind.build_notification(_cfg(app_url="https://anduin.tail.ts.net/"), "t")
    assert p["click"] == "https://anduin.tail.ts.net/log"


def test_payload_carries_no_health_data():
    p = remind.build_notification(_cfg(), "t")
    text = (p["title"] + p["message"]).lower()
    for word in ("hrv", "sleep", "intensity", "weight"):
        assert word not in text


# --- send ----------------------------------------------------------------------


@respx.mock
def test_send_posts_json_to_the_ntfy_root():
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200, json={"id": "x"}))
    with httpx.Client() as client:
        remind.send(client, _cfg(), "t0p1c", "", remind.build_notification(_cfg(), "t0p1c"))
    assert route.called
    req = route.calls.last.request
    assert req.headers["content-type"].startswith("application/json")
    assert "authorization" not in req.headers
    assert b'"topic":"t0p1c"' in req.content.replace(b" ", b"")


@respx.mock
def test_send_uses_a_bearer_token_when_configured():
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200, json={}))
    with httpx.Client() as client:
        remind.send(client, _cfg(), "t", "tk_secret", {"topic": "t"})
    assert route.calls.last.request.headers["authorization"] == "Bearer tk_secret"


# --- run -----------------------------------------------------------------------


class _Conn:
    def __init__(self, latest):
        self.latest = latest


def test_run_refuses_to_start_without_topic_or_app_url(monkeypatch):
    with httpx.Client() as client:
        assert remind.run(client, None, _app(topic=""), now=NOW, dry_run=True) == 2
        assert remind.run(client, None, _app(app_url=""), now=NOW, dry_run=True) == 2


def test_dry_run_reads_nothing_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(remind, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(remind.journal, "latest_checkin_at",
                        lambda conn, uid: (_ for _ in ()).throw(AssertionError("read")))
    with httpx.Client() as client:
        assert remind.run(client, None, _app(), now=NOW, dry_run=True) == 0


def test_wet_run_skips_inside_the_window(monkeypatch):
    sent = []
    monkeypatch.setattr(remind, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(remind.journal, "latest_checkin_at",
                        lambda conn, uid: NOW - timedelta(minutes=10))
    with httpx.Client() as client:
        assert remind.run(client, _Conn(None), _app(), now=NOW, dry_run=False) == 0
    assert sent == []


def test_wet_run_sends_and_stamps_the_configured_user(monkeypatch):
    sent, asked = [], []
    monkeypatch.setattr(remind, "send", lambda client, cfg, topic, token, payload: sent.append(payload))
    monkeypatch.setattr(remind.journal, "latest_checkin_at",
                        lambda conn, uid: asked.append(uid) or None)
    with httpx.Client() as client:
        assert remind.run(client, _Conn(None), _app(), now=NOW, dry_run=False) == 0
    assert asked == [1] and sent[0]["topic"] == "t0p1c"


def test_a_failed_send_is_a_nonzero_exit(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(remind, "send", boom)
    monkeypatch.setattr(remind.journal, "latest_checkin_at", lambda conn, uid: None)
    with httpx.Client() as client:
        assert remind.run(client, _Conn(None), _app(), now=NOW, dry_run=False) == 1


# --- CLI wiring ----------------------------------------------------------------


def test_cli_parses_remind_headache():
    args = cli._parse_args(["remind", "headache", "--dry-run"])
    assert args.cmd == "remind" and args.what == "headache" and args.dry_run is True


def test_cli_dry_run_does_not_open_db(monkeypatch):
    monkeypatch.setattr(cli.db_mod, "connect_dict",
                        lambda url: (_ for _ in ()).throw(AssertionError("opened db")))
    seen = {}

    def fake_run(http, conn, app, *, now=None, dry_run):
        seen["conn"] = conn
        return 0
    monkeypatch.setattr(cli.remind, "run", fake_run)
    rc = cli._run_remind(argparse.Namespace(what="headache", dry_run=True), _app())
    assert rc == 0 and seen["conn"] is None


def test_cli_wet_run_uses_a_dict_row_connection(monkeypatch):
    fake_conn = object()

    class _Ctx:
        def __enter__(self):
            return fake_conn

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(cli.db_mod, "connect_dict", lambda url: _Ctx())
    seen = {}

    def fake_run(http, conn, app, *, now=None, dry_run):
        seen["conn"] = conn
        return 0
    monkeypatch.setattr(cli.remind, "run", fake_run)
    rc = cli._run_remind(argparse.Namespace(what="headache", dry_run=False), _app())
    assert rc == 0 and seen["conn"] is fake_conn
