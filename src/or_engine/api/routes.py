"""api/routes.py — REST / OpenAPI endpoints over the dispatch flow.

GET   /health
POST  /workers/import      (multipart .xlsx)      -> DB
POST  /orders/import       (multipart .xlsx)      -> DB
GET   /workers
GET   /orders
POST  /dispatch            multipart: <=2 .xlsx -> JSON | xlsx
GET   /dispatch/from-db    solve everything in the DB
"""

from __future__ import annotations

import io

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..engine import dispatch
from ..models import AssignRoute, Order, Worker
from ..storage.db import DB

router = APIRouter()


async def get_db(request: Request) -> DB | None:
    """Return an open+schema-ensured DB, or ``None`` if unconfigured."""
    db = request.app.state.db
    if db is None:
        return None
    return db


def _worker_dict(w) -> dict:
    p = w.home_point
    return {
        "id": w.id,
        "name": w.name,
        "address": w.home_address,
        "lng": p.lng if p else None,
        "lat": p.lat if p else None,
        "transport": w.transport.value,
        "max_orders": w.max_orders,
    }


def _order_dict(o) -> dict:
    p = o.site_point
    tw = o.time_window
    return {
        "id": o.id,
        "no": o.order_no,
        "address": o.site_address,
        "lng": p.lng if p else None,
        "lat": p.lat if p else None,
        "window": (
            {
                "start": _hhmm(tw.start) if (tw and tw.start is not None) else None,
                "end": _hhmm(tw.end) if (tw and tw.end is not None) else None,
            }
            if tw
            else None
        ),
        "service_min": round(o.service_seconds / 60.0, 1),
    }


def _hhmm(secs: int) -> str:
    """seconds since 00:00 -> HH:MM."""
    h, rem = divmod(int(secs), 3600)
    m, _ = divmod(rem, 60)
    return f"{h:02d}:{m:02d}"


def _route_dict(r: AssignRoute) -> dict:
    return {
        "worker_id": r.worker_id,
        "worker_name": r.worker_name,
        "stop_count": len(r.assignments),
        "total_travel_min": round(r.total_travel_s / 60.0, 1),
        "total_service_min": round(r.total_service_s / 60.0, 1),
        "total_distance_km": round(r.total_distance_m / 1000.0, 1),
        "stops": [
            {
                "order_no": s.order_no,
                "sequence": s.sequence,
                "arrival": _hhmm(s.arrival_s),
                "departure": _hhmm(s.departure_s),
                "site": s.site.to_str() if s.site else None,
            }
            for s in r.assignments
        ],
    }


# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict:
    return {"status": "up"}


@router.get("/workers")
async def list_workers(request: Request) -> list[dict]:
    db = await get_db(request)
    if db is None:
        return []
    return [_worker_dict(w) for w in await db.list_workers()]


@router.get("/orders")
async def list_orders(request: Request) -> list[dict]:
    db = await get_db(request)
    if db is None:
        return []
    return [_order_dict(o) for o in await db.list_orders()]


@router.post("/workers/import")
async def import_workers(
    request: Request,
    file: UploadFile = File(...),
    source: str = Form("excel"),
) -> dict:
    db = await get_db(request)
    if db is None:
        return JSONResponse(
            {"error": "DB not configured (set DATABASE_URL)"}, status_code=503
        )
    data = await file.read()
    workers = _ingest(data, "workers", source)
    n = await db.upsert_workers(workers)
    return {"imported": n, "workers": [_worker_dict(w) for w in workers]}


@router.post("/orders/import")
async def import_orders(
    request: Request,
    file: UploadFile = File(...),
    source: str = Form("excel"),
) -> dict:
    db = await get_db(request)
    if db is None:
        return JSONResponse(
            {"error": "DB not configured (set DATABASE_URL)"}, status_code=503
        )
    data = await file.read()
    orders = _ingest(data, "orders", source)
    n = await db.upsert_orders(orders)
    return {"imported": n, "orders": [_order_dict(o) for o in orders]}


def _ingest(data: bytes, kind: str, source: str = "excel"):
    """Ingest raw .xlsx *bytes* via a temp file. Source is 'excel' only."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(data)
        path = Path(f.name)
    try:
        if kind == "workers":
            from ..excel import import_workers

            return import_workers(path)
        from ..excel import import_orders

        return import_orders(path)
    finally:
        path.unlink(missing_ok=True)


@router.post("/dispatch")
async def post_dispatch(
    request: Request,
    workers_file: UploadFile | None = File(None),
    orders_file: UploadFile | None = File(None),
    export: bool = Form(False),
    use_cache: bool = Form(True),
    source: str = Form("excel"),
    timeout_s: float = Form(30.0),
):
    """Solve a dispatch from up to two .xlsx, or from the DB, and
    optionally return the result as Excel.
    """
    db = await get_db(request)
    amap = getattr(request.app.state, "amap", None)
    workers: list[Worker] = []
    orders: list[Order] = []
    if workers_file is not None:
        workers = _ingest(await workers_file.read(), "workers", source)
    elif db is not None:
        workers = await db.list_workers()
    if orders_file is not None:
        orders = _ingest(await orders_file.read(), "orders", source)
    elif db is not None:
        orders = await db.list_orders()

    result = await dispatch(
        workers,
        orders,
        db=db,
        amap=amap,
        geocode=True,
        use_cache=use_cache,
        timeout_s=timeout_s,
    )

    out = {
        "status": result.status.value,
        "objective_value": result.objective_value,
        "solve_time_s": round(result.solve_time_s, 3),
        "assigned": len(orders) - len(result.unassigned_orders),
        "routes": [_route_dict(r) for r in result.routes],
        "unassigned": result.unassigned_orders,
        "note": result.metadata.get("note"),
    }
    if not export:
        return out

    from ..excel import export_result

    bio = io.BytesIO()
    export_result(bio, result, workers=workers, orders=orders)
    bio.seek(0)
    return StreamingResponse(
        bio,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": "attachment; filename=dispatch.xlsx"},
    )


@router.get("/dispatch/from-db")
async def dispatch_from_db(
    request: Request,
    use_cache: bool = Form(True),
    timeout_s: float = Form(30.0),
):
    """Solve using everything currently in the DB."""
    db = await get_db(request)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    workers = await db.list_workers()
    orders = await db.list_orders()
    result = await dispatch(
        workers,
        orders,
        db=db,
        amap=getattr(request.app.state, "amap", None),
        geocode=True,
        use_cache=use_cache,
        timeout_s=timeout_s,
    )
    return {
        "status": result.status.value,
        "objective_value": result.objective_value,
        "solve_time_s": round(result.solve_time_s, 3),
        "routes": [_route_dict(r) for r in result.routes],
    }
