"""engine.py — the single orchestration flow (replaces ingest/compute/output).

One async function takes workers + orders and returns a DispatchResult:

1. geocode any missing addresses to coordinates (Amap),
2. build the pairwise travel-time matrix (Amap, cached in PostGIS),
3. solve the VRPTW (OR-Tools),
4. optionally export to .xlsx.

PostGIS persistence of workers/orders and the travel-time cache is exposed
separately via the REST/MCP endpoints, so callers can persist without
running a solve.
"""

from __future__ import annotations

import logging

from .excel import export_result
from .models import DispatchResult, Order, Worker
from .solver import solve_dispatch
from .spatial.amap import AmapClient
from .spatial.travel import TravelMatrix, build_travel_matrix
from .storage.db import DB

log = logging.getLogger("or-engine")


def home_refs(workers: list[Worker]) -> list[str]:
    return [f"home:{w.id}" for w in workers]


async def geocode_missing(
    workers: list[Worker],
    orders: list[Order],
    amap: AmapClient | None,
    city: str = "",
) -> None:
    """Fill missing home_point / site_point coordinates via Amap.

    ``city`` biases AMap geocoding for free-text addresses (e.g. "上海").
    """
    if amap is None:
        return
    for w in workers:
        if w.home_point is None and w.home_address:
            hits = await amap.geocode(w.home_address, city=city)
            if hits:
                w.home_point = hits[0].location
                log.info("geocoded worker %s (%s)", w.name, w.home_point.to_str())
    for o in orders:
        if o.site_point is None and o.site_address:
            hits = await amap.geocode(o.site_address, city=city)
            if hits:
                o.site_point = hits[0].location
                log.info("geocoded order %s (%s)", o.order_no, o.site_point.to_str())


async def build_matrix(
    workers: list[Worker],
    orders: list[Order],
    db: DB | None,
    amap: AmapClient | None,
    use_cache: bool = True,
    mode: str = "driving",
) -> TravelMatrix:
    """Build the home+site travel matrix, using the PostGIS cache when given.

    A single base *driving* matrix is built; each worker's transport mode is
    applied as a per-vehicle speed factor inside the solver, so the matrix
    itself need not vary by transport.
    """
    refs = home_refs(workers) + [o.id for o in orders]
    pts = [w.home_point for w in workers] + [o.site_point for o in orders]

    missing = [refs[i] for i, p in enumerate(pts) if p is None]
    if missing:
        tail = "…" if len(missing) > 5 else ""
        raise ValueError(
            "missing coordinates for: "
            + ", ".join(missing[:5])
            + tail
            + " — geocode first"
        )
    if amap is None:
        return TravelMatrix.euclidean(pts, refs)

    return await build_travel_matrix(
        points=pts,
        refs=refs,
        client=amap,
        db=db,
        mode=mode,
        symmetric=True,
        use_cache=use_cache,
    )


async def dispatch(
    workers: list[Worker],
    orders: list[Order],
    *,
    db: DB | None = None,
    amap: AmapClient | None = None,
    geocode: bool = True,
    use_cache: bool = True,
    export_to: str | None = None,
    timeout_s: float = 30.0,
) -> DispatchResult:
    """Full flow: geocode -> cache-backed matrix -> solve -> (optional) xlsx.

    db / amap may be None (pure in-memory run with an Euclidean matrix),
    which keeps the engine runnable without any external dependency.
    """
    if geocode:
        await geocode_missing(workers, orders, amap)

    matrix = await build_matrix(
        workers,
        orders,
        db=db,
        amap=amap,
        use_cache=use_cache,
    )

    result = solve_dispatch(
        workers,
        orders,
        matrix,
        timeout_s=timeout_s,
    )

    result.metadata["matrix_source"] = matrix.source
    if export_to is not None:
        out = export_result(export_to, result, workers=workers, orders=orders)
        result.metadata["excel"] = str(out)
        log.info("wrote %s", out)

    log.info(
        "dispatch: %d workers, %d orders, %d assigned, status=%s",
        len(workers),
        len(orders),
        len(orders) - len(result.unassigned_orders),
        result.status.value,
    )
    return result
