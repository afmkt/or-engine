"""api/app.py — FastAPI application factory.

One app, two surfaces: ``/docs`` (OpenAPI) and an MCP server module that wraps
the same :mod:`or_engine.engine` functions. A shared AMap client (if a key is
configured) and a DB handle (if a URL is configured) are built once at startup
and attached to ``app.state``; routes read them.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..config import settings
from ..spatial.amap import AmapClient
from ..storage.db import DB


def create_app() -> FastAPI:
    """Build and configure the FastAPI app."""
    app = FastAPI(
        title="OR Engine",
        version="0.1.0",
        description="Single-day VRPTW dispatch: Excel → geocode → matrix → solve → Excel.",
    )
    # Optional shared clients, built lazily at startup.
    app.state.amap = (
        AmapClient(api_key=settings.amap_api_key) if settings.amap_api_key else None
    )
    app.state.db = None  # opened at startup when DATABASE_URL is set
    app.state.use_cache = True

    @app.on_event("startup")
    async def _startup():
        if settings.database_url:
            try:
                app.state.db = await DB.connect(settings.database_url)
            except Exception:  # pragma: no cover - DB is optional for testing
                app.state.db = None

    @app.on_event("shutdown")
    async def _shutdown():
        db = app.state.db
        if db is not None:
            await db.close()

    from .routes import router  # local import to cut the cycle

    app.include_router(router, tags=["dispatch"])
    return app
