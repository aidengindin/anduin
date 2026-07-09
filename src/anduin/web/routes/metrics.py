"""Metrics dashboard: an HTML page plus a JSON chart-data endpoint per metric."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg import Connection

from anduin.web import queries
from anduin.web.deps import get_conn, parse_range
from anduin.web.templating import templates

router = APIRouter()


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(
    request: Request,
    since: str | None = None,
    until: str | None = None,
) -> HTMLResponse:
    start, end = parse_range(since, until, default_days=30)
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {
            "metrics": queries.METRICS,
            "since": start.isoformat(),
            "until": (end - timedelta(days=1)).isoformat(),
        },
    )


@router.get("/api/metrics/{metric}.json")
def metric_data(
    metric: str,
    since: str | None = None,
    until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> JSONResponse:
    if metric not in queries.METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    start, end = parse_range(since, until, default_days=30)
    return JSONResponse(queries.metric_series(conn, metric, start, end))
