"""Connection pool for the web UI.

A long-running server wants a pool rather than a fresh connect per request (the
extractor path uses :func:`anduin.db.connect` for one-shot CLI runs). Opened on
FastAPI startup, closed on shutdown; a small dependency yields a connection with
a dict row factory for the duration of a request.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(dsn: str) -> ConnectionPool:
    """Create (but do not open) a small read-only pool.

    ``dict_row`` on every connection so query functions get name-keyed rows;
    ``autocommit`` -- the UI is almost entirely reads, and its one write (the
    weight goal in ``web/goals.py``) is a single statement that needs no
    transaction of its own.
    """
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
