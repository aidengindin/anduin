"""Shared Jinja2 environment + display filters.

One ``Jinja2Templates`` instance, imported by every route module, pointed at the
package's ``templates/`` directory (always on disk — nix store or src checkout —
so ``__file__`` resolution is reliable)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from anduin.sources.liftohistory import LBS_PER_KG

_TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _fmt_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.strftime(fmt) if value else "—"


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%a %d %b %Y") if value else "—"


def _fmt_dur(seconds: float | int | None) -> str:
    """Seconds → ``H:MM:SS`` / ``M:SS`` for workout durations."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _fmt_hm(minutes: float | int | None) -> str:
    """Minutes → ``7h 12m`` (drops the hour when < 1h)."""
    if minutes is None:
        return "—"
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_clock(value: datetime | None) -> str:
    """Datetime → ``11:03 PM`` (12-hour, no leading zero)."""
    if value is None:
        return "—"
    return value.strftime("%I:%M %p").lstrip("0")


def _fmt_lb(kg: float | int | None) -> str:
    """Kilograms → pounds for display, e.g. ``120`` / ``7.5``.

    Strength sets are stored in kg (the canonical unit) but logged and read in
    lb. Round-tripping the conversion leaves float noise, so round to a tenth
    and drop a trailing ``.0`` — Liftosaur's increments are all multiples of
    0.5 lb, so a tenth is enough to show 2.5/7.5 plates exactly.
    """
    if kg is None:
        return "—"
    lb = round(float(kg) * LBS_PER_KG, 1)
    return f"{lb:,.0f}" if lb == int(lb) else f"{lb:,.1f}"


def _fmt_signed(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return ""
    return f"{value:+,.{digits}f}"


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["fmt_date"] = _fmt_date
templates.env.filters["fmt_dur"] = _fmt_dur
templates.env.filters["fmt_num"] = _fmt_num
templates.env.filters["fmt_hm"] = _fmt_hm
templates.env.filters["fmt_clock"] = _fmt_clock
templates.env.filters["fmt_signed"] = _fmt_signed
templates.env.filters["fmt_lb"] = _fmt_lb
