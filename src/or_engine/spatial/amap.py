"""spatial/amap.py — thin AMap (高德地图) client.

Geocoding (address -> lat/lng) and direction / travel-time calls. Only the
two endpoints this system uses are wired. HTTP via httpx.
"""

from __future__ import annotations

import asyncio
import logging
import random
from enum import Enum

import httpx

from ..models import Point2D
from .api_error import AmapAPIError, _NON_RETRYABLE

log = logging.getLogger("or-engine")
_BASE = "https://restapi.amap.com/v3"

# AMap infocode -> human-readable meaning.  Keep this in sync with
# https://lbs.amap.com/api/webservice/download
_INFOCODES = {
    "0":     "unknown error",
    "10000": "unknown error",
    "10001": "user has no quota for this service (service not granted to this key)",
    "10002": "service is disabled for this key",
    "10003": "daily quota exhausted — re-open the AMap console to check remaining calls per day",
    "10004": "service is temporarily offline (outage on AMap's side)",
    "10009": "user QPS (queries-per-second) exceeded — slow down the request rate",
    "10010": "access forbidden — this key cannot call this service type",
    "10011": "IP address is not in the whitelist for this key",
    "10015": "daily quota exceeded (renew at midnight Beijing time)",
    "20001": "invalid parameter — check that all required fields are present and well-formed",
    "20002": "server engine internal error — retry may succeed",
    "20003": "service is temporarily unavailable — retry",
    "11001": "geocode result not found (address not indexed by AMap, try a less specific address)",
    "11002": "geocode query exceeded QPS limit for geocoding service",
    "40001": "invalid API key — check AMAP_API_KEY env var or .env file",
    "40002": "key not bound to this service — enable this service in the AMap console",
    "40003": "key type mismatch (Web Service key vs Web JS key)",
    "40004": "key daily limit exceeded",
    "40005": "key QPS limit exceeded",
    "40006": "key IP whitelist mismatch",
    "40008": "key expired",
    "10021": "CUQPS exceeded — concurrent requests/second over this key's ceiling "
             "(lower concurrency / back off and retry)",
}

# Transient / rate-limit infocodes that are safe to retry with backoff.
_RETRYABLE_INFO = frozenset({
    "10003", "10004", "10009", "10015", "10021",
    "40004", "40005", "20002", "20003",
    "30001",
})


def _info_msg(code: str | None) -> str:
    """Return a human-readable description for an AMap infocode, or a fallback."""
    if code is None:
        return "infocode not reported by API"
    return _INFOCODES.get(code, f"unknown infocode {code!r} — see https://lbs.amap.com/api/webservice/download")


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

    __slots__ = ("ok", "distance_m", "duration_s", "mode")

    def __init__(self, ok=True, distance_m=0.0, duration_s=0.0, mode=""):
        self.ok = ok
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
        max_retries: int = 5,
      ):
        self.api_key = api_key or ""
        self.timeout_s = timeout_s
        self._session = session
        self._max_retries = max_retries

    async def _get(self, path, **params) -> dict:
        url = f"{_BASE}/{path}"
        params["output"] = "json"
        if self.api_key:
            params["key"] = self.api_key

        max_attempts = self._max_retries + 1
        for attempt in range(max_attempts):
            if self._session is not None:
                r = await self._session.get(url, params=params)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_s) as s:
                    r = await s.get(url, params=params)

            # Non-200: capture HTTP status + body text + infocode before raising
            if r.status_code >= 400:
                try:
                     hb = r.json()
                     code = hb.get("infocode")
                except Exception:
                     code = None
                transient = r.status_code in (429, 500, 502, 503, 504)
                if transient and attempt + 1 < max_attempts:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise AmapAPIError(
                    f"amap http {r.status_code} {r.reason_phrase} "
                    f"path={path} url={r.request.full_url} body={r.text[:500]!r} "
                    f"           (attempt {attempt + 1}/{max_attempts}) "
                    f"         \u2192 {_info_msg(code)}",
                   http_status=r.status_code,
                   infocode=str(code) if code else None,
                   retryable=transient,
                   request_url=str(r.request.full_url),
                   )

            body = r.json()
            if body.get("status") == "1" and body.get("info") == "OK":
                return body.get("data", body)
            infocode = body.get("infocode")
            if infocode in _RETRYABLE_INFO and attempt + 1 < max_attempts:
                await asyncio.sleep(self._backoff(attempt))
                continue
            raise AmapAPIError(
                f"amap api error: info={body.get('info')!r} infocode={infocode!r} "
                f"path={path} body={body} "
                f"attempt {attempt + 1}/{max_attempts}       \u2192 {_info_msg(infocode)}",
                infocode=infocode,
                retryable=infocode not in _NON_RETRYABLE,
                request_url=f"{path}",
                 )

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff (2^attempt s, capped at 30s) + full jitter."""
        return min(2.0 ** attempt, 30.0) * (0.5 + random.random() * 0.5)

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
            raise AmapAPIError("amap: no route origin->destination", retryable=False)
        first = paths[0]
        return DirectionResponse(
            distance_m=float(first.get("distance") or 0.0),
            duration_s=float(first.get("duration") or 0.0),
            mode=m.value,
        )
