"""Combined OpenAPI (REST) + MCP server for the OR engine.

A single FastAPI application that serves:

* **REST** endpoints plus an auto-generated OpenAPI spec (``/docs``,
    ``/openapi.json``).
* an **MCP** streamable-HTTP endpoint mounted at ``/mcp``.

Because Starlette only runs the *top-level* app's lifecycle, the MCP session
manager is driven from this app's ``lifespan`` rather than from the mounted
sub-app (Starlette does not trigger the mounted app's lifespan). Skeleton: most
handlers are placeholders; ``/health`` is real.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from .mcp_server import build_mcp_server


def create_app() -> FastAPI:
    """Build the combined REST + MCP FastAPI application."""
    mcp = build_mcp_server()
    # streamable_http_path="/" so the mount below yields exactly "/mcp"
    mcp_http = mcp.streamable_http_app(streamable_http_path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Drive the MCP session manager for the whole app lifetime.
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="OR Engine", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.post("/optimize")
    async def optimize(problem: dict[str, Any]) -> dict[str, Any]:
        """Optimize a routing problem. Placeholder — solver layer is delayed."""
        raise NotImplementedError

    app.mount("/mcp", mcp_http)
    return app


# ASGI entrypoint: `uvicorn or_engine.api:app`
app = create_app()
