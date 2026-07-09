"""Request dependencies.

``get_conn`` yields a pooled connection for the life of a request. Route tests
override this via ``app.dependency_overrides[get_conn]`` to inject a fake, so
routes can be exercised without a live database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from fastapi import Request
from psycopg import Connection


def get_conn(request: Request) -> Iterator[Connection]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        yield conn


def parse_range(
    since: str | None, until: str | None, default_days: int = 30
) -> tuple[date, date]:
    """Resolve ``?since=&until=`` query params (ISO dates) to a [start, end)
    day pair. ``end`` is exclusive, so ``until`` is bumped by one day. Missing
    values default to the last ``default_days`` ending today (UTC)."""
    today = datetime.now(timezone.utc).date()
    start = date.fromisoformat(since) if since else today - timedelta(days=default_days)
    end_inclusive = date.fromisoformat(until) if until else today
    return start, end_inclusive + timedelta(days=1)
