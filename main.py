import asyncio
import os

from dotenv import load_dotenv
from or_engine import (
    AmapClient,
    Cost,
    DirectionDrivingResponse,
    Geocode,
    GeocodeResponse,
    Path,
    Point2D,
    Route,
    Step,
)


# ---------------------------------------------------------------------------
# main()  —  also callable via `or-engine`  (pyproject.toml: project.scripts)
# ---------------------------------------------------------------------------
async def main() -> None:
    load_dotenv()

    api_key = os.environ.get("AMAP_API_KEY")
    if not api_key:
        raise RuntimeError("AMAP_API_KEY not set — add it to .env and re-run")

    client = AmapClient(api_key=api_key)

    # ------------------------------------------------------------------
    # 1) Geocode:  text address  ->  coordinates
    # ------------------------------------------------------------------
    print("=== Geocode ===")
    geocode: GeocodeResponse = await client.geocode_geo("北京市朝阳区阜通东大街6号")

    if not geocode.success:
        raise RuntimeError(
            f"geocode failed: info={geocode.info!r}  infocode={geocode.infocode!r}"
         )

    for i, geo in enumerate(geocode.geocodes):
        loc = geo.location
        coords = f"{loc.lng}, {loc.lat}" if loc else "n/a"
        print(
             f"  result {i}: {geo.province} {geo.city} {geo.district}    "
             f"street={geo.street!r}  number={geo.number!r}    "
             f"level={geo.level!r}  location={coords}"
            )

    # Use the first result's coordinate as the driving origin
    origin: Point2D | None = geocode.geocodes[0].location
    assert origin is not None, "geocode returned no location for the first result"

    # ------------------------------------------------------------------
    # 2) Direction (driving):  coordinates  ->  route plan
    # ------------------------------------------------------------------
    print("\n=== Direction (driving) ===")
    destination = Point2D(lng=116.397428, lat=39.909230)   # 故宫博物院

    resp: DirectionDrivingResponse = await client.direction_driving(
        origin=origin,
        destination=destination,
        path="/v5/direction/driving",
        debug=True,        # remove after confirming the model is correct
     )

    if not resp.success:
        raise RuntimeError(
            f"direction failed: info={resp.info!r}  infocode={resp.infocode!r}"
         )

    route: Route = resp.route
    print(f"  route plans: {len(route.paths)}")
    print(f"  taxi_cost: {route.taxi_cost} yuan")
    print(f"  origin:        {route.origin.to_str()}")
    print(f"  destination:   {route.destination.to_str()}")

    for j, path in enumerate(route.paths):
        print(f"\n  --- plan {j}  "
                f"distance={path.distance} m  "
                f"restriction={path.restriction} ---")

         # cost is Optional[Cost] — present only in show_fields=cost responses
        if path.cost:
            c: Cost = path.cost
            print(f"       duration         {c.duration} s")
            print(f"       tolls            {c.tolls} yuan")
            print(f"       toll_distance    {c.toll_distance} m")
            print(f"       toll_road        {c.toll_road!r}")
            print(f"       traffic_lights   {c.traffic_lights}")

        for step in path.steps:
            navi_str = ""
            if step.navi:
                navi_str = f"   [action={step.navi.action!r}]"
            print(f"          {step.instruction:<30s}  "
                    f"({step.road_name}, {step.step_distance} m){navi_str}")


if __name__ == "__main__":
    asyncio.run(main())
