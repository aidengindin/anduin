"""Sanity tests for the migration files (presence, ordering, non-empty)."""

from __future__ import annotations

from importlib import resources


def test_migrations_present_and_ordered():
    files = resources.files("anduin.migrations")
    names = sorted(p.name for p in files.iterdir() if p.name.endswith(".sql"))
    assert names, "no migration .sql files found"
    # Lex sort matches numeric order.
    assert names == sorted(names)
    for n in names:
        body = (files / n).read_text(encoding="utf-8").strip()
        assert body, f"migration {n} is empty"


def test_migrations_idempotent_markers():
    """All schema-creating statements should use IF NOT EXISTS / OR REPLACE."""
    files = resources.files("anduin.migrations")
    for p in files.iterdir():
        if not p.name.endswith(".sql"):
            continue
        text = (files / p.name).read_text(encoding="utf-8").lower()
        for stmt in ("create table ", "create index ", "create unique index "):
            for line in text.splitlines():
                if stmt in line and "if not exists" not in line:
                    raise AssertionError(
                        f"{p.name}: `{stmt.strip()}` without IF NOT EXISTS: {line.strip()!r}"
                    )
