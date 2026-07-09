"""Read-only web UI for viewing anduin health data.

FastAPI + server-rendered Jinja2 + HTMX. Queries the reconciled ``canonical.*``
views only; never writes. Entry point is :func:`anduin.web.app.create_app`,
launched by ``anduin serve``.
"""

from __future__ import annotations
