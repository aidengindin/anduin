"""Command-line entry point.

    anduin extract <source> [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dry-run]
    anduin auth <source>
    anduin db migrate

Exits non-zero if any source reports errors.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone

from anduin import config as cfg_mod
from anduin import db as db_mod
from anduin import state as state_mod
from anduin.http import make_client
from anduin.sources import google_health, intervals, liftosaur, withings
from anduin.sources.base import SourceResult

logger = logging.getLogger("anduin")

SOURCES = ("google-health", "withings", "intervals", "liftosaur")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="anduin")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract")
    ex.add_argument("source", choices=SOURCES)
    ex.add_argument("--since", type=date.fromisoformat)
    ex.add_argument("--until", type=date.fromisoformat)
    ex.add_argument("--dry-run", action="store_true")

    au = sub.add_parser("auth")
    au.add_argument("source", choices=("google-health", "withings"))
    au.add_argument("--port", type=int, default=8765)

    dbp = sub.add_parser("db")
    dbp.add_argument("action", choices=("migrate",))

    return p.parse_args(argv)


def _default_window(days: int) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=days), today


def _run_extract(args: argparse.Namespace, app: cfg_mod.AppConfig) -> int:
    if (args.since is None) ^ (args.until is None):
        logger.error("--since and --until must be provided together")
        return 2
    result: SourceResult
    with make_client() as http, db_mod.connect(app.secrets.database_url) as conn:
        if args.source == "google-health":
            since, until = (
                (args.since, args.until)
                if args.since
                else _default_window(app.file.google_health.backfill_window_days)
            )
            result = google_health.extract(
                http, conn, app, since, until, dry_run=args.dry_run
            )
        elif args.source == "withings":
            since, until = (
                (args.since, args.until)
                if args.since
                else _default_window(app.file.withings.window_days)
            )
            result = withings.extract(http, conn, app, since, until, dry_run=args.dry_run)
        elif args.source == "intervals":
            since, until = (
                (args.since, args.until)
                if args.since
                else _default_window(app.file.intervals.window_days)
            )
            result = intervals.extract(http, conn, app, since, until, dry_run=args.dry_run)
        elif args.source == "liftosaur":
            since, until = (
                (args.since, args.until)
                if args.since
                else _default_window(app.file.liftosaur.window_days)
            )
            result = liftosaur.extract(http, conn, app, since, until, dry_run=args.dry_run)
        else:
            raise AssertionError(args.source)

    logger.info(
        "source=%s rows=%s errors=%d", result.source, result.rows_by_table, len(result.errors)
    )
    for e in result.errors:
        logger.warning("  - %s", e)
    return 1 if result.errors else 0


def _run_auth(args: argparse.Namespace, app: cfg_mod.AppConfig) -> int:
    # Lazy import; auth pulls in a tiny HTTP server.
    from anduin import oauth_flow

    if args.source == "google-health":
        token = oauth_flow.google_health_seed(
            client_id=app.secrets.google_health_client_id,
            client_secret=app.secrets.google_health_client_secret,
            redirect_port=args.port,
        )
        state_mod.save_token(app.file.state_dir, "google-health", token)
    elif args.source == "withings":
        token = oauth_flow.withings_seed(
            client_id=app.secrets.withings_client_id,
            client_secret=app.secrets.withings_client_secret,
            redirect_port=args.port,
        )
        state_mod.save_token(app.file.state_dir, "withings", token)
    else:
        raise AssertionError(args.source)
    logger.info("saved token for %s", args.source)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        app = cfg_mod.load()
    except Exception as e:  # noqa: BLE001
        logger.error("config load failed: %s", e)
        return 3

    if args.cmd == "extract":
        return _run_extract(args, app)
    if args.cmd == "auth":
        return _run_auth(args, app)
    if args.cmd == "db" and args.action == "migrate":
        n = db_mod.migrate(app.secrets.database_url)
        logger.info("applied %d migrations", n)
        return 0
    logger.error("unknown command: %s", args.cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
