"""Daily-summary landing page (``GET /``)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from psycopg import Connection

from anduin.web import queries
from anduin.web.deps import get_conn, parse_range
from anduin.web.templating import templates

router = APIRouter()


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    start, end = parse_range(since, until, default_days=30)
    days = queries.daily_summary(conn, start, end)

    # Headline tile: most recent weight seen in the window.
    latest_weight = next((d["weight_kg"] for d in days if d["weight_kg"] is not None), None)
    latest = days[0] if days else None
    # Bar chart data, oldest → newest.
    chart = [
        {"t": _epoch(d["date"]), "steps": d["steps"], "energy": d["energy_kcal"]}
        for d in reversed(days)
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "days": days,
            "latest": latest,
            "latest_weight": latest_weight,
            "chart": chart,
            "since": start.isoformat(),
            "until": (end - timedelta(days=1)).isoformat(),  # inclusive end for display
        },
    )
