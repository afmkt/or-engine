"""Standalone demo: geocode an address, then a driving direction via AMap.

uv run examples/demo_routing.py

Requires ``AMAP_API_KEY`` in the environment or a ``.env`` file.
"""

import asyncio
import os

from dotenv import load_dotenv

from or_engine import AmapClient, Point2D

load_dotenv()


async def main() -> None:
    api_key = os.environ.get("AMAP_API_KEY")
    if not api_key:
        raise RuntimeError("AMAP_API_KEY not set — add it to .env and re-run")

    client = AmapClient(api_key=api_key)

    # ── 1) Geocode: address -> coordinates ────────────────────────
    print("=== Geocode ===")
    hits = await client.geocode("北京市朝阳区阜通东大街6号")
    if not hits:
        raise RuntimeError("geocode returned no result")
    for i, g in enumerate(hits):
        print(f"  result {i}: {g.name}  @ {g.location.to_str()}")
    origin = hits[0].location

    # ── 2) Direction: coordinates -> distance + duration ───────────
    print("\n=== Direction (driving) ===")
    dest = Point2D(lng=116.397428, lat=39.909230)  # 故宫博物院
    res = await client.direction(origin, dest, mode="driving")
    print(f"  origin      : {origin.to_str()}")
    print(f"  destination : {dest.to_str()}")
    print(f"  distance    : {res.distance_m:,.0f} m")
    print(
        f"  duration    : {res.duration_s:,.0f} s "
        f"({res.duration_s / 60.0:,.1f} min)"
    )


if __name__ == "__main__":
    asyncio.run(main())
