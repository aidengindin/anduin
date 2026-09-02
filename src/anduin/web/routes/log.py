"""Log tab: headache check-ins, per-day context, and the ntfy button's API.

Plain forms, 303 on success, re-render with an inline error on bad input --
the same shape as the weight-goal editor. ``/api/log/headache`` is the one
JSON endpoint: ntfy's ``http`` action needs a 2xx, not a redirect to a page.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg import Connection

from anduin import journal
from anduin.web.deps import get_conn, user_id
from anduin.web.templating import templates

router = APIRouter()

RECENT_DAYS = 14


def _now_local() -> datetime:
    """Server-local now. The Log page's "today" and the fallback offset for
    check-ins that arrive without a browser (ntfy) both come from here, the
    same assumption the Home greeting makes."""
    return datetime.now().astimezone()


def _server_offset_minutes(now: datetime) -> int:
    off = now.utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def _parse_day(raw: str | None) -> date:
    if raw is None or raw == "":
        return _now_local().date()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad date: {raw}") from None


def _render(
    request: Request, conn: Connection, day: date,
    error: str | None = None, status_code: int = 200,
) -> HTMLResponse:
    uid = user_id(request)
    now = _now_local()
    checkins = journal.checkins_for_day(conn, uid, day)
    # Every key present so the template can compare without guards.
    context = {"fluorescent_exposure": None, "peak_intensity": None, "coffee_cups": None,
               "alcohol_drinks": None, "note": None,
               **(journal.day_context(conn, uid, day) or {})}
    checkin_peak = max((c["intensity"] for c in checkins), default=None)
    return templates.TemplateResponse(request, "log.html", {
        "active": "log",
        "day": day,
        "is_today": day == now.date(),
        "checkins": checkins,
        "checkin_peak": checkin_peak,
        "context": context,
        "recent": journal.recent_days(conn, uid, RECENT_DAYS),
        "timeline": journal.timeline_points(checkins),
        "error": error,
        "qualities": journal.QUALITIES,
        "fluorescent": journal.FLUORESCENT,
        "intensities": range(journal.INTENSITY_MAX + 1),
        "nauseas": ("none", "mild", "moderate", "severe"),
        "server_offset": _server_offset_minutes(now),
        "max_at": now.strftime("%Y-%m-%dT%H:%M"),
        "min_at": (now - timedelta(days=journal.BACKFILL_MAX_DAYS)).strftime("%Y-%m-%dT%H:%M"),
    }, status_code=status_code)


@router.get("/log", response_class=HTMLResponse)
def log_page(
    request: Request, date: str | None = None, conn: Connection = Depends(get_conn),
) -> HTMLResponse:
    return _render(request, conn, _parse_day(date))


@router.post("/log/headache", response_model=None)
async def add_checkin(
    request: Request, conn: Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    now = _now_local()
    try:
        c = journal.parse_checkin(form, now=now, server_offset_minutes=_server_offset_minutes(now))
    except journal.JournalError as exc:
        return _render(request, conn, _parse_day(form.get("date")), error=str(exc), status_code=400)
    journal.add_checkin(conn, user_id(request), c)
    return RedirectResponse(f"/log?date={c.local_date.isoformat()}", status_code=303)


@router.post("/api/log/headache")
async def add_checkin_api(
    request: Request, conn: Connection = Depends(get_conn),
) -> JSONResponse:
    """The ntfy "No headache" button. Form-encoded in, JSON out, never HTML."""
    form = dict(await request.form())
    form.setdefault("source", "ntfy")
    now = _now_local()
    try:
        c = journal.parse_checkin(form, now=now, server_offset_minutes=_server_offset_minutes(now))
    except journal.JournalError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    new_id = journal.add_checkin(conn, user_id(request), c)
    # None means a row already sat at that instant (a double tap): still fine.
    return JSONResponse({"ok": True, "id": new_id})


@router.post("/log/headache/{checkin_id}/delete", response_model=None)
async def delete_checkin(
    request: Request, checkin_id: int, conn: Connection = Depends(get_conn),
) -> RedirectResponse:
    form = await request.form()
    day = _parse_day(form.get("date"))
    journal.delete_checkin(conn, user_id(request), checkin_id)
    return RedirectResponse(f"/log?date={day.isoformat()}", status_code=303)


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@router.post("/log/day", response_model=None)
async def set_day(
    request: Request, conn: Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse | JSONResponse:
    """Per-day context. The page's autosave script POSTs the same form with
    ``Accept: application/json`` and gets a body back instead of a 303; a
    plain submit (no JS) still lands on the page."""
    form = await request.form()
    day = _parse_day(form.get("date"))
    try:
        fields = journal.parse_day_context(form)
    except journal.JournalError as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _render(request, conn, day, error=str(exc), status_code=400)
    journal.set_day_context(conn, user_id(request), day, fields)
    if _wants_json(request):
        return JSONResponse({"ok": True, "saved": fields})
    return RedirectResponse(f"/log?date={day.isoformat()}", status_code=303)
