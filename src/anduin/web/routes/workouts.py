"""Workouts: list (filterable by sport) and per-workout detail."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg import Connection

from anduin.web import queries
from anduin.web.deps import get_conn, parse_range
from anduin.web.templating import templates

router = APIRouter()


@router.get("/workouts", response_class=HTMLResponse)
def workouts_list(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    sport: str | None = None,
    conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    start, end = parse_range(since, until, default_days=90)
    sport = sport or None
    rows = queries.list_workouts(conn, start, end, sport=sport)
    available = queries.sports(conn, start, end)
    return templates.TemplateResponse(
        request,
        "workouts_list.html",
        {
            "workouts": rows,
            "sports": available,
            "selected_sport": sport,
            "since": start.isoformat(),
            "until": (end - timedelta(days=1)).isoformat(),
        },
    )


@router.get("/workouts/{source}/{activity_uid}", response_class=HTMLResponse)
def workout_detail(
    request: Request,
    source: str,
    activity_uid: str,
    conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    detail = queries.workout_detail(conn, source, activity_uid)
    if detail is None:
        raise HTTPException(status_code=404, detail="workout not found")
    return templates.TemplateResponse(request, "workout_detail.html", detail)
