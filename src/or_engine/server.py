"""Programmatic entrypoint for the OR engine.

Runs the combined REST + MCP app (:func:`or_engine.api.create_app`) under uvicorn.
The console script (``or-engine``) maps to :func:`main`.
"""

from __future__ import annotations

import argparse

import uvicorn

from .api import create_app


def main() -> None:
    """Parse CLI args and run the engine under uvicorn."""
    parser = argparse.ArgumentParser(prog="or-engine", description="OR engine server")
    parser.add_argument("--host", default="0.0.0.0", help="bind host")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    args = parser.parse_args()

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
