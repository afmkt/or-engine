"""MCP server skeleton for the OR engine (mcp 2.x ``MCPServer``).

``build_mcp_server`` returns an ``MCPServer`` exposing the engine as tools. It is
consumed two ways:

* mounted into the combined REST app at ``/mcp`` (see :mod:`or_engine.api`), or
* launched standalone over stdio / streamable-http (see :mod:`or_engine.server`).

Tools are placeholders: bodies raise ``NotImplementedError`` until the delayed
optimizer / matrix layers exist and can be wired in.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer


def build_mcp_server() -> MCPServer:
    """Construct the OR-engine MCP server with its tools registered."""
    server = MCPServer(
        name="or-engine",
        description="Operations research routing optimizer (geocoding + vehicle routing).",
    )

    @server.tool(description="Resolve a human address into geocoded coordinates.")
    async def geocode(address: str) -> dict[str, Any]:
        """Placeholder — will delegate to ``AmapClient.geocode_geo``."""
        raise NotImplementedError

    @server.tool(description="Optimize a delivery routing problem (load -> matrix -> solve -> save).")
    async def optimize(problem: dict[str, Any]) -> dict[str, Any]:
        """Placeholder — returns a routing solution once the solver is wired."""
        raise NotImplementedError

    return server
