"""FastAPI application factory.

``create_app(app_config)`` builds the UI: opens a connection pool on
startup, mounts static assets, registers routes. Launched by ``anduin serve``.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from fastapi.staticfiles import StaticFiles
from psycopg import Connection

from anduin.config import AppConfig
from anduin.web import queries
from anduin.web.db import make_pool
from anduin.web.deps import get_conn
from anduin.web.prometheus import CONTENT_TYPE, render_ingest_metrics
from anduin.web.routes import dashboard, log, metrics, workouts
from anduin.web.templating import templates

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """Short content hash across static assets, as a cache-busting token.

    Content, not mtime: the nix store stamps every file with mtime 1, so an
    mtime-based token read ``?v=1`` on every deploy and phones kept serving a
    cached app.css long after the UI had changed. Hashing the bytes bumps the
    token exactly when a vendored/CSS/JS file actually changes."""
    h = hashlib.sha256()
    try:
        for p in sorted(_STATIC_DIR.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(_STATIC_DIR).as_posix().encode())
                h.update(p.read_bytes())
    except OSError:
        return "0"
    return h.hexdigest()[:12]


def create_app(config: AppConfig) -> FastAPI:
    pool = make_pool(config.secrets.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="anduin", lifespan=lifespan)
    app.state.pool = pool
    app.state.config = config

    # Template global for cache-busting static asset URLs (?v=<mtime>).
    templates.env.globals["asset_v"] = _asset_version()

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(dashboard.router)
    app.include_router(metrics.router)
    app.include_router(log.router)
    app.include_router(workouts.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Prometheus scrape target. Lives at /-/metrics (not /metrics -- that's the
    # UI health-metrics page). Exposes per-source ingest freshness so Grafana can
    # alert when a source's data stops landing. Scrape config sets metrics_path
    # accordingly. Uses the pooled connection via get_conn so route tests can
    # override it.
    @app.get("/-/metrics", include_in_schema=False)
    def prometheus_metrics(conn: Connection = Depends(get_conn)) -> Response:
        return Response(
            content=render_ingest_metrics(queries.ingest_freshness(conn)),
            media_type=CONTENT_TYPE,
        )

    return app
