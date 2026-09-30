"""spatial/travel.py — pairwise travel-time matrix with a PostGIS cache.

For a dispatch run we need, for every (from, to) node pair, the driving
*distance* and *duration*.  Amap is slow and rate-limited, so each
computed pair is cached in the ``travel_times`` table keyed by the
'lng,lat' text of both endpoints and the transport mode.  Re-runs reuse
cached values; only new pairs hit the Amap API.

The cache key uses text refs (PostGIS ``geography`` has no btree UNIQUE),
so lookups are exact string matches on the coordinate strings.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..models import Point2D
from .amap import AmapClient, TransportMode
from .distance import build_euclidean_matrix

_ASSUMED_SPEED_MPS = 10.0  # fallback for synthetic matrices


async def build_travel_matrix(
    points: list[Point2D],
    refs: list[str],
    *,
    client: AmapClient,
    db=None,
    mode: str = "driving",
    symmetric: bool = True,
    use_cache: bool = True,
) -> "TravelMatrix":
    """Return a :class:`TravelMatrix` over ``points`` (index-aligned
    with ``refs``).

    * When ``db`` is given and ``use_cache`` is True, cached pairs are
    read from the ``travel_times`` table and missing pairs are computed
    via Amap then written back.
    * With ``symmetric=True`` (driving only, default) the triangle is
    computed once and mirrored, halving the Amap calls.
    """
    n = len(points)
    distance = [[0.0] * n for _ in range(n)]
    duration = [[0.0] * n for _ in range(n)]

    if not db or not use_cache:
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                resp = await client.direction(points[i], points[j], mode)
                if not resp.ok:
                    raise RuntimeError(
                        f"Amap direction failed {refs[i]}→{refs[j]}: "
                        f"info={resp.info!r} infocode={resp.infocode!r}"
                    )
                distance[i][j] = float(resp.distance or 0.0)
                duration[i][j] = float(resp.duration or 0.0)
        return TravelMatrix(
            node_refs=refs, distance=distance, duration=duration, source=mode
        )

    # ── cached path ────────────────────────────────────────────────
    pairs: set[tuple[int, int]] = set()
    if symmetric:
        for i in range(n):
            for j in range(i + 1, n):
                pairs.add((i, j))
    else:
        for i in range(n):
            for j in range(n):
                if i != j:
                    pairs.add((i, j))

    # 1) fill from cache where possible
    todo: set[tuple[int, int]] = set()
    for i, j in pairs:
        row = await db.get_travel_pair(points[i].to_str(), points[j].to_str(), mode)
        if row is not None:
            distance[i][j] = float(row["distance_m"] or 0.0)
            duration[i][j] = float(row["duration_s"] or 0.0)
            if symmetric:
                distance[j][i] = distance[i][j]
                duration[j][i] = duration[i][j]
        else:
            todo.add((i, j))

    # 2) compute the remainder, then write back to the cache
    for i, j in todo:
        resp = await client.direction(points[i], points[j], mode)
        if not resp.ok:
            raise RuntimeError(
                f"Amap direction failed {refs[i]}→{refs[j]}: "
                f"info={resp.info!r} infocode={resp.infocode!r}"
            )
        dm = float(resp.distance or 0.0)
        ds = float(resp.duration or 0.0)
        distance[i][j] = dm
        duration[i][j] = ds
        if symmetric:
            distance[j][i] = dm
            duration[j][i] = ds
        await db.save_travel_pair(
            from_ref=points[i].to_str(),
            to_ref=points[j].to_str(),
            mode=mode,
            distance_m=distance[i][j],
            duration_s=duration[i][j],
        )
        return TravelMatrix(
            node_refs=refs, distance=distance, duration=duration, source=mode
        )


class TravelMatrix:
    """A fully materialised pairwise distance/duration matrix.

    Index-aligned with ``node_refs``; also indexable by node label via
    :meth:`lookup`.
    """

    def __init__(
        self,
        node_refs: list[str],
        distance: list[list[float]],
        duration: list[list[float]],
        source: str = "amap",
    ) -> None:
        self.node_refs = node_refs
        self.distance = distance
        self.duration = duration
        self.source = source
        self._idx = {r: i for i, r in enumerate(node_refs)}

    @property
    def n(self) -> int:
        return len(self.node_refs)

    @classmethod
    def euclidean(cls, points: list[Point2D], refs: list[str]) -> "TravelMatrix":
        """Synthetic matrix (no Amap): Euclidean distance, fixed speed.
        Used for local testing when no Amap key is available."""
        d = build_euclidean_matrix(points)
        dur = [[r / _ASSUMED_SPEED_MPS for r in row] for row in d]
        return cls(node_refs=refs, distance=d, duration=dur, source="euclidean")

    def lookup(self, a: str, b: str) -> tuple[float, float]:
        return self.distance[self._idx[a]][self._idx[b]]

    def as_records(self):
        """Flatten to (i, j, distance, duration) for debugging / storage."""
        for i in range(self.n):
            for j in range(self.n):
                if i != j:
                    yield SimpleNamespace(
                        i=i,
                        j=j,
                        distance=self.distance[i][j],
                        duration=self.duration[i][j],
                    )
