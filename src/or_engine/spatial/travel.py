"""spatial/travel.py -- pairwise travel-time matrix with a PostGIS/SQLite cache.

For a dispatch run we need, for every (from, to) node pair, the driving
*distance* and *duration*.  Amap is slow and rate-limited, so each computed
pair is cached in the ``travel_times`` table keyed by the 'lng,lat' text of
both endpoints and the transport mode.  Re-runs reuse cached values; only new
pairs hit the Amap API.

The cache key uses text refs (PostGIS ``geography`` has no btree UNIQUE), so
lookups are exact string matches on the coordinate strings.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

from ..models import Point2D
from .amap import AmapClient, TransportMode
from .distance import build_euclidean_matrix

_ASSUMED_SPEED_MPS = 10.0    # fallback for synthetic matrices

# -- internal helpers --------------------------------------------------------

def _progress(i: int, total: int, last_from: str, last_to: str,
              dm: float, ds: float) -> None:
    """Print a 1-line progress marker for every completed pair."""
    if total <= 0:        return
    print(f"            [amap] direction   {i:>6d}/{total} "
          f"({100*i/total:5.1f}%)    "
          f"last: {last_from}->{last_to}    "
          f"dist={dm:.0f}m  dur={ds:.0f}s", flush=True)


async def _compute_pairs_amap(
    pairs,
    *,
    points: list[Point2D],
    refs: list[str],
    client: AmapClient,
    mode: str,
    symmetric: bool,
    concurrency: int = 5,
    cache=None,
    distance: list[list[float]],
    duration: list[list[float]],
) -> None:
    """Compute travel times for *pairs*, filling *distance*/*duration* in-place.

    Uses a semaphore of size *concurrency* via asyncio.Semaphore.
    *cache* is any object with an async ``save_travel_pair`` method
    (e.g. DB or LocalCache); pass None to skip the write-back.
    """
    total = len(pairs)
    if total == 0:
        return

    completed = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)

    async def _one(i: int, j: int) -> None:
        nonlocal completed
        async with sem:
            resp = await client.direction(points[i], points[j], mode)
            if not resp.ok:
                raise RuntimeError(
                    f"Amap direction failed {refs[i]}->{refs[j]}: "
                    f"info={resp.info!r} infocode={resp.infocode!r}")
            dm = float(resp.distance_m or 0.0)
            ds = float(resp.duration_s or 0.0)
            async with lock:
                distance[i][j] = dm
                duration[i][j] = ds
                if symmetric:
                    distance[j][i] = dm
                    duration[j][i] = ds
                completed += 1
                _progress(completed, total, refs[i], refs[j], dm, ds)

            # Optionally write back to cache (sequential within lock)
            if cache is not None:
                await cache.save_travel_pair(
                    from_ref=points[i].to_str(),
                    to_ref=points[j].to_str(),
                    mode=mode,
                    distance_m=dm,
                    duration_s=ds,
                 )
                print(f"              [cache] INSERT direction    "
                      f"{refs[i]} -> {refs[j]}   "
                      f"dist={dm:.0f}m  dur={ds:.0f}s", flush=True)

    print(f"            [amap] direction  starting {total} pairs   "
          f"(N={len(points)}, symmetric={symmetric}, "
          f"concurrency={concurrency})")
    await asyncio.gather(*(_one(i, j) for i, j in pairs))


async def build_travel_matrix(
    points: list[Point2D],
    refs: list[str],
    *,
    client: AmapClient,
    db=None,
    local_cache=None,
    mode: str = "driving",
    symmetric: bool = True,
    use_cache: bool = True,
    concurrency: int = 5,
    synthetic_coords: "set[str] | None" = None,
) -> "TravelMatrix":
    """Return a :class:`TravelMatrix` over ``points`` (index-aligned
    with ``refs``).

    * When *db* or *local_cache* is given and ``use_cache`` is True,
      cached pairs are read first and missing pairs are computed via
      Amap then written back to whichever cache is configured
      (PostGIS *db* or local SQLite *local_cache*).
    * With ``symmetric=True`` (driving only, default) the triangle is
      computed once and mirrored, halving the Amap calls.
    * ``concurrency`` controls the asyncio.Semaphore for parallel API calls.
    * ``synthetic_coords`` -- a set of 'lng,lat' endpoint strings that came
      from the local synthetic fallback (not a real AMap geocode).  When
      given, the scan summary reports how many nodes/pairs are backed by that
      fallback data source, so the AMap-vs-fallback split stays visible.
    """
    cache = db or local_cache
    n = len(points)
    distance = [[0.0] * n for _ in range(n)]
    duration = [[0.0] * n for _ in range(n)]

    # -- no cache: compute all needed pairs ---------------------------
    if not cache or not use_cache:
        if symmetric:
            all_pairs = list(itertools.combinations(range(n), 2))
        else:
            all_pairs = [(i, j) for i in range(n)
                         for j in range(n) if i != j]

        await _compute_pairs_amap(
            all_pairs, points=points, refs=refs, client=client,
            mode=mode, symmetric=symmetric, cache=None,
            distance=distance, duration=duration,
        )
        return TravelMatrix(
            node_refs=refs, distance=distance, duration=duration, source=mode,
        )

    # -- cached path --------------------------------------------------
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

    total_pairs = len(pairs)

    # 1) fill from cache where possible
    todo: list[tuple[int, int]] = []
    cache_hits = 0
    for i, j in pairs:
        from_ref = points[i].to_str()
        to_ref = points[j].to_str()
        row = await cache.get_travel_pair(from_ref, to_ref, mode)
        if row is not None:
            distance[i][j] = float(row["distance_m"] or 0.0)
            duration[i][j] = float(row["duration_s"] or 0.0)
            if symmetric:
                distance[j][i] = distance[i][j]
                duration[j][i] = duration[i][j]
            cache_hits += 1
            print(f"              [cache] HIT   direction    "
                  f"{refs[i]} -> {refs[j]}   "
                  f"dist={distance[i][j]:.0f}m  dur={duration[i][j]:.0f}s",
                  flush=True)
        else:
            todo.append((i, j))
            print(f"              [cache] MISS  direction    "
                  f"{refs[i]} -> {refs[j]}   (will call AMap)",
                  flush=True)

    # 2) data-source split: an endpoint from the local synthetic fallback
    #    (not a real AMap geocode) yields unreliable routing -- report how
    #    many scanned nodes and needed pairs touch such a fallback endpoint.
    _synth = synthetic_coords or set()
    if _synth:
        synth_nodes = [refs[i] for i in range(n)
                       if points[i].to_str() in _synth]
        miss_touching = sum(1 for (i, j) in todo
                            if points[i].to_str() in _synth
                            or points[j].to_str() in _synth)
    else:
        synth_nodes, miss_touching = [], 0

    # post-scan / pre-fetch summary -- printed BEFORE any Amap call so the
    # user can see the cache status (hits vs misses) for the direction matrix.
    cache_label = "postgis" if db is not None else "sqlite"
    _loc = getattr(cache, "_path", None)
    _locstr = f"{cache_label}:{_loc}" if _loc else cache_label
    _pct = (100 * cache_hits / total_pairs) if total_pairs else 0.0
    _block: list[str] = [
        f"            [cache] direction  scan complete     [{_locstr}]",
        f"                    scanned            : {total_pairs} pair(s)      "
            f"(symmetric={symmetric})",
        f"                    HIT      (in cache): {cache_hits:<8d} "
            f"({_pct:5.1f}%)",
        f"                    MISS (need AMap) : {len(todo):<8d} "
            f"({100 - _pct:5.1f}%)",
    ]
    if _synth:
        _block += [
            f"                    -- data source --                 ",
            f"                    AMap geocode     : {n - len(synth_nodes)} node(s)",
            f"                    LOCAL fallback   : {len(synth_nodes)} node(s) "
                f"[NOT from AMap]",
            f"                    MISS touches local: {miss_touching} pair(s)",
        ]
        for r in synth_nodes:
            _block.append(f"                             - (local) {r}")
    _block.append(
        "                    next                  "
        + (f"fetch {len(todo)} missing pair(s) via Amap "
           f"(concurrency={concurrency})" if todo
           else "none (matrix fully cached, 0 AMap calls)")
     )
    print("\n".join(_block), flush=True)

    # 3) compute missing pairs, then write back to cache
    await _compute_pairs_amap(
        todo, points=points, refs=refs, client=client,
        mode=mode, symmetric=symmetric, cache=cache,
        distance=distance, duration=duration,
    )
    return TravelMatrix(
        node_refs=refs, distance=distance, duration=duration, source=mode,
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
