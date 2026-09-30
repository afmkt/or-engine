"""spatial/amap.py — thin AMap (高德地图) client.

Geocoding (address -> lat/lng) and direction / travel-time calls. Only the
two endpoints this system uses are wired. HTTP via httpx.
"""

from __future__ import annotations

import logging
from enum import Enum

import httpx

from ..models import Point2D

log = logging.getLogger("or-engine")
_BASE = "https://restapi.amap.com/v3"


def _parse_geom(geom: str) -> Point2D:
    lng, lat = geom.split(",")
    return Point2D(lat=float(lat), lng=float(lng))


class Geocode:
    """A geocoding hit: a resolved address + coordinates."""

    __slots__ = ("name", "location", "adcode")

    def __init__(self, name: str, location: Point2D, adcode: str | None):
        self.name = name
        self.location = location
        self.adcode = adcode

    def __repr__(self) -> str:
        return f"Geocode({self.name!r}, {self.location})"


class DirectionResponse:
    """A single direction result: distance (m) + duration (s)."""

    __slots__ = ("distance_m", "duration_s", "mode")

    def __init__(self, distance_m, duration_s, mode=""):
        self.distance_m = distance_m
        self.duration_s = duration_s
        self.mode = mode

    def __repr__(self) -> str:
        return "DirectionResponse(dur=%.0fs dist=%.0fm %s)" % (
            self.duration_s,
            self.distance_m,
            self.mode,
        )


class TransportMode(str, Enum):
    # AMap direction modes. Distinct from the worker transport enum in
    # or_engine.models despite the shared word.
    DRIVING = "driving"
    RIDING = "riding"
    WALKING = "walking"

    @classmethod
    def coerce(cls, v):
        if v is None:
            return cls.DRIVING
        if isinstance(v, cls):
            return v
        key = str(v).strip().lower()
        if key in ("0", "driv", "driving", "car", "车"):
            return cls.DRIVING
        if key in ("1", "cycl", "riding", "ebike", "e-bike", "电瓶车", "bike"):
            return cls.RIDING
        if key in ("3", "walk", "walking", "步行"):
            return cls.WALKING
        return cls.DRIVING


class AmapClient:
    """Async AMap client for geocoding + direction.

    The travel-time API (``amap.direction``) is the only thing we hit on the
    hot path; geocoding is one-shot per address at ingest.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        session: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key or ""
        self.timeout_s = timeout_s
        self._session = session

    async def _get(self, path, **params) -> dict:
        url = f"{_BASE}/{path}"
        params["output"] = "json"
        if self.api_key:
            params["key"] = self.api_key
        if self._session is not None:
            r = await self._session.get(url, params=params)
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as s:
                r = await s.get(url, params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") == "1" and body.get("info") == "OK":
            return body.get("data", body)
        raise RuntimeError(f"amap api error: {body.get('info')} / {path}")

    async def geocode(self, address: str, city: str = "") -> list[Geocode]:
        """Turn a free-text address into coordinates (top hits)."""
        if not self.api_key:
            raise ValueError("AMAP_API_KEY is not set")
        body = await self._get("geocode/geo", address=address, city=city)
        out = []
        for c in body.get("geocodes") or []:
            loc = c.get("location")
            if not loc:
                continue
            out.append(Geocode(c.get("name", ""), _parse_geom(loc), None))
        return out

    async def direction(self, origin, destination, mode="driving"):
        """Route origin -> destination: distance (m) + duration (s)."""
        m = TransportMode.coerce(mode)
        body = await self._get(
            f"direction/{m.value}",
            origin=origin.to_str(),
            destination=destination.to_str(),
        )
        route = body.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            raise RuntimeError("amap: no route origin->destination")
        first = paths[0]
        return DirectionResponse(
            distance_m=float(first.get("distance") or 0.0),
            duration_s=float(first.get("duration") or 0.0),
            mode=m.value,
        )
