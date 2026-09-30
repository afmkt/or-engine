"""In-memory smoke test: a tiny dispatch with NO external dependencies.

uv run examples/smoke_test.py

Builds two workers and three time-windowed orders, solves the VRPTW with a
synthetic (Euclidean) travel matrix — no AMap key, no Postgres — and prints
the resulting routes. Set ``USE_DB=1`` (plus ``DB_URL`` / ``AMAP_API_KEY``) to
exercise the PostGIS travel-time cache and DB persistence instead.
"""

import asyncio
import os

from dotenv import load_dotenv

from or_engine import Order, Point2D, TimeWindow, TransportMode, Worker
from or_engine.engine import dispatch
from or_engine.excel import export_result

load_dotenv()


def sample_orders():
    """Three time-windowed installation orders around Shanghai."""
    return [
        Order(
            id="o1",
            order_no="A001",
            site_address="南京东路",
            site_point=Point2D(lng=121.49, lat=31.24),
            time_window=TimeWindow.from_hhmm("08:30", "18:00"),
        ),
        Order(
            id="o2",
            order_no="A002",
            site_address="静安寺",
            site_point=Point2D(lng=121.44, lat=31.22),
            time_window=TimeWindow.from_hhmm("09:00", "17:00"),
        ),
        Order(
            id="o3",
            order_no="A003",
            site_address="世纪公园",
            site_point=Point2D(lng=121.56, lat=31.19),
            time_window=TimeWindow.from_hhmm("10:00", "18:00"),
        ),
    ]


def sample_workers():
    """Two installation workers: one fast car, one e-bike."""
    return [
        Worker(
            id="w1",
            name="Zhang",
            transport=TransportMode.CAR_SH,
            home_point=Point2D(lng=121.50, lat=31.24),
            max_orders=3,
        ),
        Worker(
            id="w2",
            name="Li",
            transport=TransportMode.EBIKE,
            home_point=Point2D(lng=121.45, lat=31.21),
            max_orders=3,
        ),
    ]


async def main() -> None:
    db = None
    amap = None
    out_xlsx = None
    use_cache = False

    if os.environ.get("USE_DB") == "1":
        from or_engine.spatial.amap import AmapClient
        from or_engine.storage.db import DB

        db = DB(os.environ.get("DB_URL"))
        await db._connect()
        await db.ensure_schema()
        use_cache = True
        if os.environ.get("AMAP_API_KEY"):
            amap = AmapClient(api_key=os.environ["AMAP_API_KEY"])
        out_xlsx = "dispatch.xlsx"

    workers = sample_workers()
    orders = sample_orders()

    result = await dispatch(
        workers, orders, db=db, amap=amap, geocode=False, use_cache=use_cache
    )

    print(
        f"status={result.status.value}   "
        f"solved in {result.solve_time_s:.3f}s   "
        f"matrix={result.metadata.get('matrix_source')}\n"
    )
    for r in result.routes:
        print(
            f"   {r.worker_name}: {len(r.assignments)} stops, "
            f"{r.total_travel_s/60.0:.1f} min travel, "
            f"{r.total_distance_m/1000.0:.1f} km"
        )
        for s in r.assignments:
            hh = s.arrival_s // 3600
            mm = s.arrival_s % 3600 // 60
            print(
                f"       #{s.sequence}  {s.order_no}  "
                f"arrives {hh:02d}:{mm:02d}  "
                f"@ {s.site.to_str()}"
            )
    if result.unassigned_orders:
        print("\nUNASSIGNED:", result.unassigned_orders)

    if out_xlsx:
        export_result(out_xlsx, result, workers=workers, orders=orders)
        print(f"\nwrote {out_xlsx}")


if __name__ == "__main__":
    asyncio.run(main())
