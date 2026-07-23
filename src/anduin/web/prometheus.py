"""Prometheus text-exposition rendering for the /-/metrics endpoint.

Kept as a pure function (rows in, exposition text out) so it unit-tests without a
database or HTTP layer. We hand-roll the format rather than pull in
``prometheus_client`` -- there are only two gauges and the escaping rules for the
handful of label values we emit are trivial.
"""

from __future__ import annotations

from typing import Any, Iterable

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value: backslash, double-quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_ingest_metrics(rows: Iterable[dict[str, Any]]) -> str:
    """Render per-source ingest freshness as Prometheus exposition text.

    ``rows`` come from ``queries.ingest_freshness`` -- each has ``source``,
    ``last_ingest_epoch`` and ``lag_seconds``. Emits the absolute timestamp
    gauge (the canonical form -- alert on ``time() - <ts>`` in PromQL so the
    signal doesn't go stale between scrapes) plus a convenience lag gauge.
    """
    rows = list(rows)
    lines: list[str] = [
        "# HELP anduin_source_last_ingest_timestamp_seconds "
        "Unix time of the most recent row ingested per source.",
        "# TYPE anduin_source_last_ingest_timestamp_seconds gauge",
    ]
    for r in rows:
        src = _escape_label(str(r["source"]))
        lines.append(
            f'anduin_source_last_ingest_timestamp_seconds{{source="{src}"}} '
            f'{int(r["last_ingest_epoch"])}'
        )
    lines.append(
        "# HELP anduin_source_ingest_lag_seconds "
        "Seconds since the most recent ingested row per source."
    )
    lines.append("# TYPE anduin_source_ingest_lag_seconds gauge")
    for r in rows:
        src = _escape_label(str(r["source"]))
        lines.append(f'anduin_source_ingest_lag_seconds{{source="{src}"}} {int(r["lag_seconds"])}')
    return "\n".join(lines) + "\n"
