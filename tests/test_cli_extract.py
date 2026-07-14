"""CLI extract wiring: --dry-run must not require a database connection.

Validating a source against its API (e.g. an empty-response smoke pull) should
not need a writable DB. This guards that dry-run passes conn=None and never
calls db.connect.
"""

from __future__ import annotations

import argparse

from anduin import cli
from anduin.config import AppConfig, FileConfig, Secrets
from anduin.sources import base


def _app():
    return AppConfig(secrets=Secrets(database_url="postgresql://dummy/anduin"),
                     file=FileConfig())


def _args(dry_run):
    return argparse.Namespace(source="google-health", since=None, until=None, dry_run=dry_run)


def test_dry_run_does_not_open_db(monkeypatch):
    seen = {"conn": "unset"}

    def fake_connect(url):  # noqa: ANN001
        raise AssertionError("dry-run must not open a DB connection")

    def fake_extract(http, conn, app, since, until, *, dry_run):  # noqa: ANN001
        seen["conn"] = conn
        return base.SourceResult(source="google_health")

    monkeypatch.setattr(cli.db_mod, "connect", fake_connect)
    monkeypatch.setattr(cli.google_health, "extract", fake_extract)

    rc = cli._run_extract(_args(dry_run=True), _app())

    assert rc == 0
    assert seen["conn"] is None
