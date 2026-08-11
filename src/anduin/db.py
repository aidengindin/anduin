"""Postgres connection + migration runner.

Thin wrapper around psycopg. Migrations are plain .sql files in the package's
`migrations/` directory, applied in lexicographic order. State tracked in
`anduin_meta.schema_migrations`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

import psycopg
from psycopg import Connection

logger = logging.getLogger(__name__)


@contextmanager
def connect(dsn: str) -> Iterator[Connection]:
    with psycopg.connect(dsn, autocommit=False) as conn:
        yield conn


def _ensure_meta(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS anduin_meta;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS anduin_meta.schema_migrations (
                version     text PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now()
            );
            """
        )
    conn.commit()


def _applied(conn: Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM anduin_meta.schema_migrations;")
        return {row[0] for row in cur.fetchall()}


def _migration_files() -> list[tuple[str, str]]:
    """Return [(name, sql), ...] sorted lex by name."""
    out: list[tuple[str, str]] = []
    files = resources.files("anduin.migrations")
    for entry in sorted(p.name for p in files.iterdir() if p.name.endswith(".sql")):
        sql = (files / entry).read_text(encoding="utf-8")
        out.append((entry, sql))
    return out


def refresh_activity_daily(conn: Connection) -> None:
    """Recompute the canonical.activity_daily materialized rollup.

    Called after each ingest — the rollup only changes when new raw data lands.
    CONCURRENTLY keeps web reads on the old contents instead of blocking, but
    cannot run inside a transaction, hence the autocommit flip.
    """
    conn.commit()
    prev = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY canonical.activity_daily;")
    finally:
        conn.autocommit = prev


def migrate(dsn: str) -> int:
    """Apply pending migrations. Returns number applied."""
    applied_n = 0
    with connect(dsn) as conn:
        _ensure_meta(conn)
        already = _applied(conn)
        for name, sql in _migration_files():
            if name in already:
                continue
            logger.info("applying migration %s", name)
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO anduin_meta.schema_migrations (version) VALUES (%s);",
                    (name,),
                )
            conn.commit()
            applied_n += 1
    return applied_n
