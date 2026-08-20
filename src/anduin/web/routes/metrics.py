"""Metrics: grouped index, per-metric detail, sleep detail, JSON chart data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg import Connection

from anduin.web import goals, queries
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


@router.get("/metrics/blood_pressure", response_class=HTMLResponse)
def blood_pressure_page(request: Request, conn: Connection = Depends(get_conn)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "blood_pressure.html",
        {"active": "metrics", "detail": queries.blood_pressure_detail(conn)},
    )


def _user_id(request: Request) -> int:
    """Owner of every row. Single-user today; the id is configured, never
    defaulted at the DB level (see CLAUDE.md)."""
    return request.app.state.config.file.user_id


def _render_metric_page(
    request: Request, conn: Connection, metric: str,
    since: str | None, until: str | None,
    goal_error: str | None = None, status_code: int = 200,
) -> HTMLResponse:
    start, end = parse_range(since, until, default_days=30)
    detail = queries.metric_detail(conn, metric)
    today = datetime.now(timezone.utc).date()
    span = (end - timedelta(days=1) - start).days
    ranges = [
        {"label": lbl, "since": (today - timedelta(days=d)).isoformat(),
         "on": abs(span - d) <= 2}
        for lbl, d in (("1W", 7), ("1M", 30), ("3M", 90), ("1Y", 365))
    ]
    ctx = {
        "active": "metrics", "detail": detail, "metric": metric, "ranges": ranges,
        "since": start.isoformat(), "until": (end - timedelta(days=1)).isoformat(),
    }
    # Body weight is the only metric carrying a goal: it is the one the owner
    # steers day to day, and the corridor only makes sense against a rate target.
    if metric == "body_weight":
        goal = goals.current_goal(conn, _user_id(request))
        ctx["goal"] = goal
        ctx["goal_status"] = queries.weight_goal_status(conn, goal)
        ctx["goal_error"] = goal_error
        ctx["goal_kinds"] = goals.KINDS
    return templates.TemplateResponse(
        request, "metric_detail.html", ctx, status_code=status_code,
    )


@router.post("/metrics/body_weight/goal", response_model=None)
async def set_weight_goal(
    request: Request, conn: Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """The one write in the UI. Plain urlencoded form, no JavaScript.

    Invalid input re-renders the page with the message inline rather than
    returning a bare 422 the browser would show as a wall of JSON."""
    form = await request.form()
    try:
        kind, target = goals.parse_goal(str(form.get("kind", "")), form.get("target"))
    except goals.GoalError as exc:
        return _render_metric_page(
            request, conn, "body_weight", None, None,
            goal_error=str(exc), status_code=400,
        )
    goals.set_goal(conn, _user_id(request), kind, target)
    return RedirectResponse("/metrics/body_weight", status_code=303)


@router.get("/metrics/{metric}", response_class=HTMLResponse)
def metric_page(
    request: Request, metric: str,
    since: str | None = None, until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    if metric not in queries.METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    return _render_metric_page(request, conn, metric, since, until)


@router.get("/api/metrics/{metric}.json")
def metric_data(
    request: Request, metric: str, since: str | None = None, until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> JSONResponse:
    if metric not in queries.METRICS:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    start, end = parse_range(since, until, default_days=30)
    goal = goals.current_goal(conn, _user_id(request)) if metric == "body_weight" else None
    return JSONResponse(queries.metric_series(conn, metric, start, end, goal))
