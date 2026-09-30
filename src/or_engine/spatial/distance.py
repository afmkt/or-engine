"""Distance calculations."""

from __future__ import annotations

import math

from ..models import Point2D

_EARTH_RADIUS_M = 6_371_000.0
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LNG_BASE = 111_320.0


def haversine(a: Point2D, b: Point2D) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lng - a.lng)
    hav = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_M * 2 * math.asin(math.sqrt(hav))


def euclidean_m(a: Point2D, b: Point2D) -> float:
    """Planar distance in metres (good for areas < 100 km)."""
    mx = (b.lng - a.lng) * _M_PER_DEG_LNG_BASE * math.cos(math.radians(a.lat))
    my = (b.lat - a.lat) * _M_PER_DEG_LAT
    return math.hypot(mx, my)


def build_euclidean_matrix(nodes: list[Point2D]) -> list[list[float]]:
    """Symmetric NxN Euclidean distance matrix in metres."""
    n = len(nodes)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = euclidean_m(nodes[i], nodes[j])
            dist[i][j] = dist[j][i] = d
    return dist
