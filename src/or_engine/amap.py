"""Amap (高德) Web Service API models and client.

Public classes:
    Point2D              — coordinate type, shared by requests and responses
    Geocode / GeocodeResponse      — geocoding API
    DirectionDrivingResponse  / ... — driving-direction API
    AmapClient           — async HTTP client, wraps both APIs
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, model_validator
from typing import Any


# ---------------------------------------------------------------------------
# Point2D — unified coordinate type used across requests and responses
# ---------------------------------------------------------------------------
class Point2D(BaseModel):
    """A geographic coordinate.

    The API returns coordinates as "lng,lat" strings.  Point2D wraps that and
    acts as the canonical input type for any function that takes a coordinate.

        Point2D(lng=116.48, lat=39.99)
        Point2D.from_str("116.48,39.99")
        Point2D.from_tuple((116.48, 39.99))
    """

    lng: float
    lat: float

    # -- construction helpers -------------------------------------------------

    @classmethod
    def from_str(cls, s: str) -> "Point2D":
        """Parse the 'lng,lat' string that Amap returns."""
        parts = s.split(",")
        if len(parts) != 2:
            raise ValueError(f"Invalid coordinate string: {s!r}")
        return cls(lng=float(parts[0]), lat=float(parts[1]))

    @classmethod
    def from_tuple(cls, pair: tuple[float, float]) -> "Point2D":
        return cls(lng=float(pair[0]), lat=float(pair[1]))

    # -- serialization helpers ------------------------------------------------

    def to_str(self) -> str:
        """Serialize back to 'lng,lat' for API requests."""
        return f"{self.lng},{self.lat}"

    def to_tuple(self) -> tuple[float, float]:
        return (self.lng, self.lat)

    # -- universal coercion ---------------------------------------------------
    # Accepts Point2D, a "lng,lat" str, a [lng, lat] list/tuple, or None.
    # Used by every model validator and by API request builders.

    @classmethod
    def coerce(cls, v: Any) -> "Point2D | None":
        if v is None:
            return None
        if isinstance(v, str):
            return cls.from_str(v) if v.strip() else None
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return cls.from_tuple(tuple(v))
        if isinstance(v, cls):
            return v
        raise TypeError(f"Cannot convert {type(v).__name__} to Point2D")


# ---------------------------------------------------------------------------
# Direction-driving response models
# ---------------------------------------------------------------------------
class DirectionDrivingResponse(BaseModel):
    status: str            # "1" = success, "0" = failure
    info: str              # "OK" on success, error message on failure
    infocode: str          # "10000" on success, error code on failure
    count: str             # total number of route plans, as string
    route: "Route | None" = None

    @property
    def success(self) -> bool:
        return self.status == "1"

    @property
    def route_count(self) -> int:
        return int(self.count) if self.count else 0


class Route(BaseModel):
    origin: Point2D        # auto-parsed from "lng,lat" via model_validator
    destination: Point2D   # auto-parsed from "lng,lat" via model_validator
    taxi_cost: str         # estimated taxi fare in yuan, e.g. "15.00"
    paths: list["Path"] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_coordinates(cls, data: Any) -> Any:
        """Convert raw 'lng,lat' strings to Point2D before type validation."""
        if isinstance(data, dict):
            for key in ("origin", "destination"):
                if key in data:
                    data[key] = Point2D.coerce(data[key])
        return data


class Path(BaseModel):
    distance: str          # total distance in metres
    restriction: str       # "0" = unrestricted, "1" = restricted

    # --- fields available when show_fields includes "cost" ---
    cost: "Cost | None" = None
    # --- fields available when show_fields includes "tmcs" ---
    tmcs: "list[Tmc] | None" = None
    # --- fields available when show_fields includes "cities" ---
    cities: "list[CityInfo] | None" = None

    steps: list["Step"] = []

    @property
    def distance_meters(self) -> float | None:
        return float(self.distance) if self.distance else None


class Cost(BaseModel):
    duration: str          # total driving time in seconds
    tolls: str             # toll fee in yuan
    toll_distance: str     # total toll road length in metres
    toll_road: str         # main toll road name
    traffic_lights: str    # number of traffic lights

    @property
    def duration_seconds(self) -> int | None:
        return int(self.duration) if self.duration else None

    @property
    def tolls_yuan(self) -> float | None:
        return float(self.tolls) if self.tolls else None


class Tmc(BaseModel):
    tmc_status: str        # "未知" | "畅通" | "缓行" | "拥堵" | "严重拥堵"
    tmc_distance: str      # distance of this traffic segment in metres
    tmc_polyline: str      # coordinate poly-line for this segment

    @property
    def tmc_distance_meters(self) -> float | None:
        return float(self.tmc_distance) if self.tmc_distance else None


class District(BaseModel):
    name: str
    adcode: str


class CityInfo(BaseModel):
    adcode: str
    citycode: str
    city: str
    district: "District | None" = None


class Navi(BaseModel):
    action: str            # primary navigation action
    assistant_action: str  # secondary / assistant navigation action


class Step(BaseModel):
    # All string fields default to "" so the model tolerates steps that omit
    # some fields (e.g. tunnel/turn-only steps lack road_name and orientation).
    instruction:     str = ""   # driving instruction text
    orientation:     str = ""   # direction when entering the road
    road_name:       str = ""   # road name (sometimes omitted by the API)
    step_distance:   str = ""   # distance of this step in metres

    # --- fields available when show_fields includes "navi" / "polyline" ---
    navi:     "Navi | None" = None
    polyline: "str | None"   = None   # coordinate points separated by ";"

    @property
    def step_distance_meters(self) -> float | None:
        return float(self.step_distance) if self.step_distance else None


# Resolve forward references
Route.model_rebuild()
DirectionDrivingResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Geocode response models
# ---------------------------------------------------------------------------
class Geocode(BaseModel):
    country:    str        = ""
    province:   str        = ""
    city:       str        = ""
    citycode:   str        = ""
    district:   str        = ""
    street:     str        = ""
    number:     str        = ""
    adcode:     str        = ""
    location:   "Point2D | None" = None   # auto-parsed from "lng,lat"
    level:      str        = ""           # 匹配级别, e.g. "门牌号", "道路", "兴趣点"

    @model_validator(mode="before")
    @classmethod
    def _coerce_location(cls, data: Any) -> Any:
        if isinstance(data, dict) and "location" in data:
            data["location"] = Point2D.coerce(data["location"])
        return data


class GeocodeResponse(BaseModel):
    status:   str             # "0" or "1"
    info:     str             # "OK" or error message
    count:    str             # number of results, as string
    geocodes: list[Geocode] = []

    @property
    def success(self) -> bool:
        return self.status == "1"

    @property
    def result_count(self) -> int:
        return int(self.count) if self.count else 0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class AmapClient:
    """Async client for the Amap Web Service API."""

    def __init__(self, api_key: str, api_base: str = "https://restapi.amap.com"):
        self.api_key  = api_key
        self.api_base = api_base

    def _params(self, path: str, **params: Any) -> tuple[str, dict[str, Any]]:
        return (f"{self.api_base}{path}", {**params, "key": self.api_key})

     # -- geocoding ----------------------------------------------------------

    async def geocode_geo(
        self,
        address: str,
        path: str  = "/v3/geocode/geo",
        debug: bool = False,
    ) -> GeocodeResponse:
        url, params = self._params(path, address=address)
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params)
            raw = res.json()
            if debug:
                print(json.dumps(raw, indent=2, ensure_ascii=False))
            return GeocodeResponse.model_validate(raw)

     # -- direction (driving) ------------------------------------------------

    async def direction_driving(
        self,
        origin:      "Point2D | str | tuple[float, float] | None",
        destination: "Point2D | str | tuple[float, float] | None",
        path: str  = "/v5/direction/driving",
        debug: bool = False,
    ) -> DirectionDrivingResponse:
        origin_pnt      = Point2D.coerce(origin)
        destination_pnt = Point2D.coerce(destination)
        url, params = self._params(
            path,
            origin=origin_pnt.to_str()        if origin_pnt        is not None else None,
            destination=destination_pnt.to_str() if destination_pnt is not None else None,
        )
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params)
            raw = res.json()
            if debug:
                print(json.dumps(raw, indent=2, ensure_ascii=False))
            return DirectionDrivingResponse.model_validate(raw)
