"""Home — the 1a sleep-hero landing page (``GET /``)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from psycopg import Connection

from anduin.web import queries
from anduin.web.deps import get_conn
from anduin.web.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn: Connection = Depends(get_conn)) -> HTMLResponse:
    data = queries.home(conn)
    now = datetime.now(timezone.utc)
    hour = now.hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "active": "home",
            "greeting": greeting,
            "today": now,
            "sleep_goal_min": queries.SLEEP_GOAL_MIN,
            **data,
        },
    )
