"""Metrics: grouped index, per-metric detail, sleep detail, JSON chart data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg import Connection

from anduin.web import queries
from anduin.web.deps import get_conn, parse_range
from anduin.web.templating import templates

router = APIRouter()


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request, conn: Connection = Depends(get_conn)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "metrics.html",
        {"active": "metrics", "groups": queries.metric_index(conn)},
    )


@router.get("/sleep", response_class=HTMLResponse)
def sleep_page(request: Request, conn: Connection = Depends(get_conn)) -> HTMLResponse:
    detail = queries.sleep_detail(conn)
    return templates.TemplateResponse(
        request, "sleep_detail.html", {"active": "metrics", "detail": detail},
    )


@router.get("/metrics/{metric}", response_class=HTMLResponse)
def metric_page(
    request: Request, metric: str,
    since: str | None = None, until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    if metric not in queries.METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    start, end = parse_range(since, until, default_days=30)
    detail = queries.metric_detail(conn, metric)
    today = datetime.now(timezone.utc).date()
    span = (end - timedelta(days=1) - start).days
    ranges = [
        {"label": lbl, "since": (today - timedelta(days=d)).isoformat(),
         "on": abs(span - d) <= 2}
        for lbl, d in (("1W", 7), ("1M", 30), ("3M", 90), ("1Y", 365))
    ]
    return templates.TemplateResponse(
        request, "metric_detail.html",
        {
            "active": "metrics", "detail": detail, "metric": metric, "ranges": ranges,
            "since": start.isoformat(), "until": (end - timedelta(days=1)).isoformat(),
        },
    )


@router.get("/api/metrics/{metric}.json")
def metric_data(
    metric: str, since: str | None = None, until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> JSONResponse:
    if metric not in queries.METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    start, end = parse_range(since, until, default_days=30)
    return JSONResponse(queries.metric_series(conn, metric, start, end))
