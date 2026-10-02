"""api/routes.py -- REST / OpenAPI endpoints over the dispatch flow.

Ingestion (each stores the raw .xlsx as bytes + a sha256 content hash in
``file_uploads`` and links the parsed rows back via ``source_upload_id``):

POST   /workers/import        multipart .xlsx -> workers       + file_uploads
POST   /orders/import         multipart .xlsx -> orders        + file_uploads
POST   /tasks/import          multipart .xlsx -> orders        + file_uploads
POST   /working-hours/import  multipart .xlsx -> working_hours + file_uploads

Listing:
GET    /health
GET    /workers
GET    /orders
GET    /working-hours
GET    /uploads[?kind=...]

Solving:
POST   /dispatch              multipart: <=2 .xlsx -> JSON | xlsx
GET    /dispatch/from-db      solve everything in the DB
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from ..excel import import_orders, import_tasks, import_working_hours, import_workers
from ..engine import dispatch
from ..models import AssignRoute, Order, WorkingHour, Worker
from ..storage.db import DB

router = APIRouter()

# accepted ingestion kinds
_KINDS = {"workers", "orders", "tasks", "working_hours"}


async def get_db(request: Request) -> DB | None:
    """Return an open+schema-ensured DB, or None if unconfigured."""
    db = request.app.state.db
    return db if db is not None else None


# ---- (de)serialisation ------------------------------------------------------
def _worker_dict(w: Worker) -> dict:
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


def _order_dict(o: Order) -> dict:
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


def _working_hour_dict(h: WorkingHour) -> dict:
    return {"product": h.product, "hours": h.hours, "note": h.note}


def _hhmm(secs: int | None) -> str:
    """seconds since 00:00 -> HH:MM."""
    if secs is None:
        return ""
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


def _upload_dict(record: dict) -> dict:
    """A compact, API-safe view of a file_uploads record (no bytes)."""
    return {
        "id": record["id"],
        "kind": record.get("kind"),
        "filename": record.get("filename"),
        "sha256": record["sha256"],
        "size_bytes": record.get("size_bytes"),
        "deduplicated": record.get("deduplicated", False),
        "uploaded_at": str(record["uploaded_at"]) if record.get("uploaded_at") else None,
    }


def _tmp(data: bytes) -> Path:
    """Write raw bytes to a temp .xlsx path for the synchronous importers."""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(data)
        return Path(f.name)


# ---- ingestion --------------------------------------------------------------
def _parse(data: bytes, kind: str) -> list:
    """Parse *kind* from .xlsx *data* into domain rows."""
    if kind == "workers":
        return import_workers(_tmp(data))
    if kind in ("orders", "tasks"):
        # 'tasks' is the on-disk name for the work-order sheet
        return import_orders(_tmp(data), "tasks" if kind == "tasks" else None)
    if kind == "working_hours":
        return import_working_hours(_tmp(data))
    raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")


def _cleanup(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _ingest(request: Request, kind: str, upload: UploadFile) -> tuple[list, dict]:
    """Store raw bytes + hash in file_uploads, parse to rows, upsert into the
    DB linked by source_upload_id. Returns (rows, upload_record)."""
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    db = await get_db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="DB not configured (set DATABASE_URL)")

    data = await upload.read()
      # 1) persist the raw file + its content hash
    record = await db.save_upload(
        kind=kind,
        payload=data,
        filename=upload.filename or f"{kind}.xlsx",
        content_type=getattr(upload, "content_type", None),
    )
    upload_id = record["id"]

      # 2) parse the raw bytes into domain rows
    rows = _parse(data, kind)

      # 3) upsert the parsed rows, tagged with the upload id for traceability
    if kind == "workers":
        await db.upsert_workers(rows, upload_id)
    elif kind in ("orders", "tasks"):
        await db.upsert_orders(rows, upload_id)
    else:  # working_hours
        await db.upsert_working_hours(rows, upload_id)
    return rows, record


# ---- health -----------------------------------------------------------------
@router.get("/health")
async def health() -> dict:
    return {"status": "up"}


# ---- listing ----------------------------------------------------------------
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


@router.get("/working-hours")
async def list_working_hours(request: Request) -> list[dict]:
    db = await get_db(request)
    if db is None:
        return []
    return [_working_hour_dict(h) for h in await db.list_working_hours()]


@router.get("/uploads")
async def list_uploads(request: Request, kind: str | None = None) -> list[dict]:
    db = await get_db(request)
    if db is None:
        return []
    return await db.list_uploads(kind)


# ---- ingestion endpoints (store bytes + hash, then parse + upsert) ----------
@router.post("/workers/import")
async def import_workers_ep(request: Request, file: UploadFile = File(...)) -> dict:
    rows, record = await _ingest(request, "workers", file)
    return {
        "upload": _upload_dict(record),
        "imported": len(rows),
        "workers": [_worker_dict(w) for w in rows],
    }


@router.post("/orders/import")
async def import_orders_ep(request: Request, file: UploadFile = File(...)) -> dict:
    rows, record = await _ingest(request, "orders", file)
    return {
        "upload": _upload_dict(record),
        "imported": len(rows),
        "orders": [_order_dict(o) for o in rows],
    }


@router.post("/tasks/import")
async def import_tasks_ep(request: Request, file: UploadFile = File(...)) -> dict:
    """Ingest a tasks.xlsx (work orders). Parsed into the orders table."""
    rows, record = await _ingest(request, "tasks", file)
    return {
        "upload": _upload_dict(record),
        "imported": len(rows),
        "orders": [_order_dict(o) for o in rows],
    }


@router.post("/working-hours/import")
async def import_working_hours_ep(request: Request, file: UploadFile = File(...)) -> dict:
    rows, record = await _ingest(request, "working_hours", file)
    return {
        "upload": _upload_dict(record),
        "imported": len(rows),
        "working_hours": [_working_hour_dict(h) for h in rows],
    }


# ---- solving ----------------------------------------------------------------
@router.post("/dispatch")
async def post_dispatch(
    request: Request,
    workers_file: UploadFile | None = File(None),
    orders_file: UploadFile | None = File(None),
    export: bool = Form(False),
    use_cache: bool = Form(True),
    timeout_s: float = Form(30.0),
):
    """Solve a dispatch from up to two .xlsx, or from the DB, and optionally
    return the result as Excel."""
    db = await get_db(request)
    amap = getattr(request.app.state, "amap", None)
    workers: list[Worker] = []
    orders: list[Order] = []
    if workers_file is not None:
         # transient solve: parse only, no upload-row recording
        wdata = await workers_file.read()
        workers = _parse(wdata, "workers")
    elif db is not None:
        workers = await db.list_workers()
    if orders_file is not None:
        odata = await orders_file.read()
        orders = _parse(odata, "orders")
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
    use_cache: bool = Query(True),
    timeout_s: float = Query(30.0),
):
    """Solve using everything currently in the DB."""
    db = await get_db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="DB not configured")
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
