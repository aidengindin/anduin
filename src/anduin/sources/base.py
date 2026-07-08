"""Common types for source extractors.

Each extractor produces a SourceResult: a count of rows landed per raw table,
plus any errors. The extractor writes directly to Postgres; the result is the
audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SourceResult:
    source: str
    rows_by_table: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, table: str, n: int) -> None:
        self.rows_by_table[table] = self.rows_by_table.get(table, 0) + n

    def error(self, msg: str) -> None:
        self.errors.append(msg)
