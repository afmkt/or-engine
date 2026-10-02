"""api/mcp.py — Model Context Protocol server, exposing the dispatch flow.

Thin wrapper over :mod:`or_engine.engine`. Uses ``mcp`` (FastMCP) when the
package is installed; otherwise it degrades to a plain async CLI so the
underlying logic stays runnable without the MCP dependency.

Tools: import_workers / import_orders / list / dispatch.
"""

from __future__ import annotations

import json
import sys

from ..config import settings
from ..storage.db import DB

try:
    from mcp.server.fastmcp import FastMCP

    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False


async def open_context():
    """Return ``(db, amap)`` from env. Both may be ``None`` for in-memory
    runs (DB/AMap are optional)."""
    from ..spatial.amap import AmapClient

    db = None
    amap = None
    if settings.database_url:
        db = await DB.connect(settings.database_url)
    if settings.amap_api_key:
        amap = AmapClient(api_key=settings.amap_api_key)
    return db, amap


async def op_import(path: str, kind: str = "workers", source: str = "excel"):
    """Ingest an .xlsx at *path* into the DB, recording the raw file.

    ``kind`` is one of 'workers' / 'orders' / 'tasks' / 'working_hours'.
    Returns {'upload': <file_uploads record>, 'imported': <row count>}; the
    parsed rows are linked back to the stored bytes via source_upload_id.
    """
    import os

    from ..excel import (
        import_orders,
        import_tasks,
        import_working_hours,
        import_workers,
    )

    db, _ = await open_context()
    if db is None:
        raise RuntimeError("DB not configured (set DATABASE_URL env)")

    # 1) record the raw bytes + content hash so parsed rows trace back to source
    with open(path, "rb") as fh:
        payload = fh.read()
    record = await db.save_upload(
        kind=kind,
        payload=payload,
        filename=os.path.basename(path),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    upload_id = record["id"]

    # 2) excel importers are synchronous and take a path
    if kind == "workers":
        rows, upsert = import_workers(path), lambda: db.upsert_workers(rows, upload_id)
    elif kind in ("orders", "tasks"):
        rows = import_tasks(path) if kind == "tasks" else import_orders(path)
        upsert = lambda: db.upsert_orders(rows, upload_id)
    elif kind == "working_hours":
        rows = import_working_hours(path)
        upsert = lambda: db.upsert_working_hours(rows, upload_id)
    else:
        raise ValueError(f"unknown import kind {kind!r}")

    n = await upsert()
    return {
        "upload": {
            "id": record["id"],
            "kind": kind,
            "filename": os.path.basename(path),
            "sha256": record["sha256"],
            "size_bytes": record.get("size_bytes"),
            "deduplicated": record.get("deduplicated", False),
        },
        "imported": n,
    }


async def op_dispatch(
    workers_file: str | None = None,
    orders_file: str | None = None,
    *,
    use_cache: bool = True,
    source: str = "excel",
    timeout_s: float = 30.0,
    out_xlsx: str | None = None,
) -> dict:
    """Run a dispatch and return a JSON-serialisable summary + optional xlsx."""
    from ..engine import dispatch as engine_dispatch
    from ..excel import export_result, import_orders, import_workers

    db, amap = await open_context()
    if workers_file:
        workers = import_workers(workers_file)
    elif db is not None:
        workers = await db.list_workers()
    else:
        workers = []
    if orders_file:
        orders = import_orders(orders_file)
    elif db is not None:
        orders = await db.list_orders()
    else:
        orders = []

    res = await engine_dispatch(
        workers,
        orders,
        db=db,
        amap=amap,
        geocode=(amap is not None),
        use_cache=use_cache,
        timeout_s=timeout_s,
    )
    if out_xlsx:
        from ..excel import export_result

        out_path = export_result(out_xlsx, res, workers=workers, orders=orders)
        res.objective_value = res.objective_value  # keep summary shape stable
    out_data: dict = {
        "status": res.status.value,
        "solve_time_s": round(res.solve_time_s or 0.0, 3),
        "assigned": len(orders) - len(res.unassigned_orders),
        "total_travel_min": round(sum(r.total_travel_s for r in res.routes) / 60.0, 1),
        "routes": [
            {"worker_name": r.worker_name, "stops": len(r.assignments)}
            for r in res.routes
        ],
        "unassigned": res.unassigned_orders,
    }
    if out_xlsx:
        out_data["xlsx"] = str(out_xlsx)
    return out_data


async def op_list(kind: str = "workers") -> list[dict]:
    """List current 'workers' / 'orders' / 'working_hours' from the DB."""
    db, _ = await open_context()
    if db is None:
        return []
    if kind == "workers":
        rows = await db.list_workers()
        return [
            {
                "id": w.id,
                "name": w.name,
                "address": w.home_address,
                "transport": w.transport.value,
                "lng": w.home_point.lng if w.home_point else None,
                "lat": w.home_point.lat if w.home_point else None,
            }
            for w in rows
        ]
    if kind == "working_hours":
        rows = await db.list_working_hours()
        return [
              {"product": h.product, "hours": h.hours, "note": h.note}
            for h in rows
           ]
          # "orders" and "tasks" share the orders table
    rows = await db.list_orders()
    return [
        {
            "id": o.id,
            "no": o.order_no,
            "address": o.site_address,
            "lng": o.site_point.lng if o.site_point else None,
            "lat": o.site_point.lat if o.site_point else None,
        }
        for o in rows
    ]


# ---------------------------------------------------------------------------
# MCP surface (FastMCP) — guarded so a missing `mcp` dep doesn't break the CLI.
# ---------------------------------------------------------------------------
if _HAS_MCP:
    mcp = FastMCP("or-engine")

    @mcp.tool(name="import_workers", description="Ingest a workers .xlsx into the DB")
    async def mcp_import_workers(path: str, source: str = "excel"):
        return await op_import(path, "workers", source)

    @mcp.tool(name="import_orders", description="Ingest an orders .xlsx into the DB")
    async def mcp_import_orders(path: str, source: str = "excel"):
        return await op_import(path, "orders", source)

    @mcp.tool(name="import_tasks", description="Ingest a tasks .xlsx (work orders) into the DB")
    async def mcp_import_tasks(path: str, source: str = "excel"):
        return await op_import(path, "tasks", source)

    @mcp.tool(name="import_working_hours", description="Ingest a working_hours .xlsx (product -> labour hours)")
    async def mcp_import_working_hours(path: str, source: str = "excel"):
        return await op_import(path, "working_hours", source)

    @mcp.tool(
        name="list",
        description="List current workers, orders/tasks, or working_hours from the DB",
       )
    async def mcp_list(kind: str = "workers"):
        return await op_list(kind)

    @mcp.tool(
        name="dispatch",
        description="Solve a single-day VRPTW; return a summary; optional xlsx out",
    )
    async def mcp_dispatch(
        workers_file: str = None,
        orders_file: str = None,
        use_cache: bool = True,
        out_xlsx: str = None,
    ):
        return await op_dispatch(
            workers_file,
            orders_file,
            use_cache=use_cache,
            out_xlsx=out_xlsx,
        )


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI: ``dispatch -w <workers.xlsx> -o <orders.xlsx>``."""
    import argparse
    import asyncio

    p = argparse.ArgumentParser(
        prog="or-engine", description="Single-day dispatch (VRPTW)"
    )
    p.add_argument("-w", "--workers", help="workers .xlsx")
    p.add_argument("-o", "--orders", help="orders .xlsx")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("-x", "--xlsx-out", help="write the result to this .xlsx")
    args = p.parse_args(argv)

    async def run():
        res = await op_dispatch(
            workers_file=args.workers,
            orders_file=args.orders,
            use_cache=not args.no_cache,
            out_xlsx=args.xlsx_out,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))

    asyncio.run(run())
    return 0


if __name__ == "__main__" and _HAS_MCP:
    mcp.run()
elif __name__ == "__main__":
    sys.exit(main())
