"""Unit tests for the Prometheus exposition renderer (pure, no DB/HTTP)."""

from __future__ import annotations

from anduin.web.prometheus import render_ingest_metrics


def test_render_emits_both_gauges_per_source():
    rows = [
        {"source": "google_health", "last_ingest_epoch": 1_700_000_000, "lag_seconds": 3600},
        {"source": "withings", "last_ingest_epoch": 1_699_000_000, "lag_seconds": 90000},
    ]
    out = render_ingest_metrics(rows)
    # HELP/TYPE headers present for both gauges
    assert "# TYPE anduin_source_last_ingest_timestamp_seconds gauge" in out
    assert "# TYPE anduin_source_ingest_lag_seconds gauge" in out
    # one sample line per source per gauge, integer-valued
    assert 'anduin_source_last_ingest_timestamp_seconds{source="google_health"} 1700000000' in out
    assert 'anduin_source_ingest_lag_seconds{source="google_health"} 3600' in out
    assert 'anduin_source_ingest_lag_seconds{source="withings"} 90000' in out
    # valid exposition ends with a trailing newline
    assert out.endswith("\n")


def test_render_empty_is_headers_only():
    out = render_ingest_metrics([])
    assert "anduin_source_last_ingest_timestamp_seconds{" not in out
    assert out.endswith("\n")


def test_render_escapes_label_values():
    out = render_ingest_metrics(
        [{"source": 'we"ird\\', "last_ingest_epoch": 1, "lag_seconds": 2}]
    )
    assert 'source="we\\"ird\\\\"' in out
