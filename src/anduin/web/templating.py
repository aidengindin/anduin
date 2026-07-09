"""Shared Jinja2 environment + display filters.

One ``Jinja2Templates`` instance, imported by every route module, pointed at the
package's ``templates/`` directory (always on disk — nix store or src checkout —
so ``__file__`` resolution is reliable)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

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


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["fmt_date"] = _fmt_date
templates.env.filters["fmt_dur"] = _fmt_dur
templates.env.filters["fmt_num"] = _fmt_num
