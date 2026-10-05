"""Standalone single-day dispatch pipeline.

A CLI entry point that runs the *whole* one-day VRPTW-with-drop
pipeline via file transform, an AMap geocode + driving-matrix call
(with optional SQLite caching via ``--cache <path>`` -- reuses
geocode/travel results across runs without a database server),
and the OR-Tools optimiser:

      1 parse-hours   working_hours.xlsx           -> product -> hours lookup
      2 parse-workers workers.xlsx                 -> Worker[]
      3 parse-tasks   tasks.xlsx + hours lookup   -> kept Order[] + removed/qty reports
      4 geocode       AMap (city) or synth auto   -> coordinates
      5 matrix        AMap driving (symmetric)     -> pairwise distance/duration matrix
      6 solve         OR-Tools VRPTW-with-drop      -> DispatchResult
      7 export        result.xlsx (+ 移除 + 数量警告 sheets)

Each step is its own sub-command and writes its output to a JSON checkpoint in
``--workdir`` (default ``./_run``), so a step can be re-run / inspected alone.
``run`` chains them all.  With no ``AMAP_API_KEY`` the coordinate / matrix steps
fall back to deterministic *synthetic* coords / a Euclidean matrix, so the demo
still runs end-to-end offline (clearly labelled).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dotenv import load_dotenv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .excel import export_result, import_workers
from .models import DispatchResult, Order, Point2D, Worker
from .spatial.amap import AmapClient, AmapAPIError
from .spatial.api_error import record_geocode_failure, record_direction_failure
from .spatial.failures import FailureTracker
from .spatial.travel import TravelMatrix, build_travel_matrix
from .storage.cache import LocalCache
from .solver import solve_dispatch
from .tasks import RemovedTask, import_tasks, load_working_hours

# -- defaults (project decisions) -------------------------------------------
DEFAULT_CITY = "上海"
DEFAULT_DAY_START = "08:00"
DEFAULT_DAY_END = "18:00"
DEFAULT_WARN_QTY = 5
DEFAULT_PENALTY_BASE = 1e7        # large -> "schedule as many as possible" first
DEFAULT_PER_HOUR_K = 1.0          # additive penalty per service-second (drop ∝ time)
_SH_LAT = (31.00, 31.45)          # synthetic-geocode bounding box for 上海
_SH_LNG = (121.30, 121.65)


def hhmm_seconds(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


def hms(s: int) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def transport_name(w: Worker) -> str:
    return w.transport.value if hasattr(w.transport, "value") else str(w.transport)


# -- in-memory pipeline state -----------------------------------------------
@dataclass
class State:
    lookup: dict | None = None
    unknown: list = field(default_factory=list)
    workers: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    qty_warnings: list = field(default_factory=list)
    matrix: object | None = None
    result: object | None = None
    coords_source: str = "amap"
    matrix_source: str = ""
    # nodes whose coordinates came from the local synthetic fallback rather
    # than a real AMap geocode, so AMap-vs-fallback is preserved across the
    # pipeline. Each entry: {kind, key, address, coords("lng,lat")}.
    fallbacks: list = field(default_factory=list)
    failures: FailureTracker = field(default_factory=FailureTracker)


# -- checkpoint I/O to --workdir --------------------------------------------
def _save(path: Path, step: str, st: State) -> None:
    path.mkdir(parents=True, exist_ok=True)

    def w(name, obj):
         (path / f"{name}.json").write_text(
             json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                 encoding="utf-8")

    if step == "lookup":
        w("lookup", {"lookup": st.lookup, "unknown": st.unknown})
    elif step == "workers":
        w("workers_raw", [v.model_dump(mode="json") for v in st.workers])
    elif step == "tasks":
        w("tasks_raw", [o.model_dump(mode="json") for o in st.orders])
        w("removed", [t.as_dict() for t in st.removed])
        w("qty_warnings", st.qty_warnings)
    elif step == "geo":
        w("workers_geo", [v.model_dump(mode="json") for v in st.workers])
        w("tasks_geo", [o.model_dump(mode="json") for o in st.orders])
        w("meta", {"coords_source": st.coords_source,
                        "fallbacks": st.fallbacks})
        # machine-readable data-source split: which nodes were NOT geocoded
         # via AMap but got local/synthetic fallback coordinates. Downstream
         # tooling can read --workdir/fallbacks.json to filter unreliable legs.
        fbs = st.fallbacks
        fb_nw = sum(1 for f in fbs if f["kind"] == "worker")
        fb_no = sum(1 for f in fbs if f["kind"] == "order")
        w("fallbacks", {
                "coords_source": st.coords_source,
               "count": len(fbs),
               "workers": fb_nw,
               "orders": fb_no,
               "all_from_amap": len(fbs) == 0,
               "fallbacks": [
                    {"kind": f["kind"], "key": f["key"],
                         "address": f["address"], "coords": f["coords"],
                         "lng": float(f["coords"].split(",")[0]),
                         "lat": float(f["coords"].split(",")[1]),
                         "reason": "amap geocode miss/error -> synthetic"}
                    for f in fbs],
             })
        print(f"             [cache] wrote {path / 'fallbacks.json'}     "
                f"({len(fbs)} local-fallback node(s); "
                f"{len(fbs) == 0 and 'all coords from AMap' or 'see file'})",
                flush=True)
    elif step == "matrix" and st.matrix is not None:
        m = st.matrix
        w("matrix", {"node_refs": m.node_refs, "distance": m.distance,
                       "duration": m.duration, "source": m.source})
    elif step == "result" and st.result is not None:
        w("result", st.result.model_dump(mode="json"))
         # persist failure records across runs
    if st.failures:
        st.failures.save_path(path)



def _load(path: Path) -> State:
    st = State()

    def rd(name):
        p = path / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    d = rd("lookup")
    if d:
        st.lookup = d.get("lookup") or {}
        st.unknown = d.get("unknown") or []
    # orders: prefer the GEODED checkpoints (real coords) over raw
    orders = rd("tasks_geo") or rd("tasks_raw")
    if orders is not None:
        st.orders = [Order(**x) for x in orders]
        st.removed = [
             RemovedTask(order_no=str(x.get("order_no", "")), reason=x.get("reason", ""),
                         lines=x.get("lines") or [], merchant=x.get("merchant"))
             for x in (rd("removed") or [])]
        st.qty_warnings = rd("qty_warnings") or []
    m = dict(rd("meta") or {})
    st.coords_source = m.get("coords_source", "amap")
    # prefer the dedicated machine-readable fallbacks.json; fall back to
     # the legacy meta payload for older checkpoints.
    fb_doc = rd("fallbacks")
    if fb_doc is not None:
        st.fallbacks = fb_doc.get("fallbacks") or []
        st.coords_source = fb_doc.get("coords_source", st.coords_source)
    else:
        st.fallbacks = m.get("fallbacks") or []
    # workers: prefer geocoded
    workers = rd("workers_geo") or rd("workers_raw")
    if workers is not None:
        st.workers = [Worker(**x) for x in workers]
    d = rd("matrix")
    if d:
        st.matrix = TravelMatrix(node_refs=d["node_refs"], distance=d["distance"],
                                  duration=d["duration"], source=d.get("source", "amap"))
    d = rd("result")
    if d and isinstance(d, dict):
        try:
            st.result = DispatchResult.model_validate(d)
        except Exception:
            st.result = None      # export will re-run solve if needed
        # load cross-run API failure records
    try:
        st.failures = FailureTracker.load_path(path)
    except Exception:
        pass
    return st


# -- step 4: geocode --------------------------------------------------------
def _synth_coords(addr: str) -> Point2D:
    """Deterministic pseudo-geocode into the 上海 bounding box (offline fallback).

    Must be stable across processes: the built-in ``hash()`` of a str is
    salted per interpreter (PYTHONHASHSEED), so a coordinate derived from it
    changes every run and breaks the travel-time cache keys (``to_str()``)
    -- those pairs then MISS the cache on every re-run. Use a stable digest.
    """
    import hashlib
    h = int(hashlib.md5((addr or "?").encode("utf-8")).hexdigest(), 16) % 100000
    lat = _SH_LAT[0] + (h % 10000) / 10000.0 * (_SH_LAT[1] - _SH_LAT[0])
    lng = _SH_LNG[0] + (h // 10000) / 1000.0 * (_SH_LNG[1] - _SH_LNG[0])
    return Point2D(lat=round(lat, 6), lng=round(lng, 6))


async def _one_geocode(client: AmapClient, addr: str, city: str,
                       cache: "LocalCache | None" = None,
                       stats: "dict | None" = None,
                       tracker: "FailureTracker | None" = None):
    """"Call AMap geocode for one address, logging each outcome live.

            Returns the first Geocode hit, or None on miss / HTTP / parse error.
            When *stats* is a dict, per-address cache outcomes are tallied in
            place (hit / miss / saved / failed) for the end-of-phase summary.
            """

    def _bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    # skip blocked (non-retryable) failures from prior runs
    key = f"{addr}|{city}"
    if tracker is not None and tracker.is_blocked("geocode", key):
        rec = tracker.lookup("geocode", key)
        _bump("skipped_blocked")
        tag = rec.reason if rec else "non-retryable"
        print(f" [replay] SKIP {addr!r} city={city!r} ({tag})", flush=True)
        return None

    if cache is not None:
        hit = await cache.get_geocode(addr, city)
        if hit is not None:
            _bump("hit")
            print(f"             [cache]   HIT       {addr!r}   city={city!r}"
                                f"      -> ({hit.location.lat}, {hit.location.lng})", flush=True)
            return hit
        _bump("miss")
        print(f"             [cache]   MISS      {addr!r}   city={city!r}"
                      f"      (will call AMap)", flush=True)

    print(f"             [amap]      ->  geocode  {addr!r}   city={city!r}",
                  flush=True)
    try:
        hits = await client.geocode(addr, city=city)

    except AmapAPIError as e:
        # record it; non-retryable fails get skipped next run
        if tracker is not None:
            record_geocode_failure(tracker, addr, city, e)
        _bump("failed")
        tag = "RETRY" if e.retryable else "NO-RETRY"
        print(f" <- ERROR infocode={e.infocode} [{tag}] {e.message}", flush=True)
        return None
    except Exception as e:                               # surface, do not abort
        _bump("failed")
        print(f"               <-  ERROR    {type(e).__name__}: {e}", flush=True)
        return None
    if hits:
        g = hits[0]
        extra = f"     [ +{len(hits) - 1} more ]" if len(hits) > 1 else ""
        print(f"               <-  OK     {g.name or '?':<18}"
                      f" ({g.location.lat}, {g.location.lng}){extra}", flush=True)
        if cache is not None:
            await cache.save_geocode(addr, city, g.name or "",
                                                 g.location.lat, g.location.lng)
            _bump("saved")
            print(f"             [cache] INSERT     {addr!r}   city={city!r}"
                                  f"      <- ({g.location.lat}, {g.location.lng})", flush=True)
        return g
    _bump("failed")
    print("               <-  MISS  no geocode returned", flush=True)
    return None


async def _geocode_amap(st: State, city: str,
                        fallback: bool = False,
                        cache: 'LocalCache | None' = None,
                        stats: 'dict | None' = None) -> tuple[int, int, int]:
    """Geocode every missing point via AMap, logging each call to the console.

    fallback=True -> synthetic coords for any miss/error (keeps the run going).
    Returns (resolved, failed, synthesised); resolved counts points whose
    coordinate is set after the pass.
    """
    client = AmapClient(api_key=os.environ.get("AMAP_API_KEY", ""))
    failed = synthesised = 0
    for w in st.workers:
        if w.home_point is not None:
            continue
        if w.home_address:
            hit = await _one_geocode(client, w.home_address, city,
                                     cache=cache, stats=stats,
                                     tracker=st.failures)
            if hit is not None:
                w.home_point = hit.location
        if w.home_point is None:
            if fallback:
                w.home_point = _synth_coords(w.home_address or w.name or "d")
                st.fallbacks.append({"kind": "worker", "key": w.id,
                         "address": w.home_address or w.name or "d",
                         "coords": w.home_point.to_str()})
                synthesised += 1
            else:
                failed += 1
    for o in st.orders:
        if o.site_point is not None:
            continue
        if o.site_address:
            hit = await _one_geocode(client, o.site_address, city,
                                     cache=cache, stats=stats,
                                     tracker=st.failures)
            if hit is not None:
                o.site_point = hit.location
        if o.site_point is None:
            if fallback:
                o.site_point = _synth_coords(o.site_address or o.order_no or "s")
                st.fallbacks.append({"kind": "order", "key": o.order_no,
                          "address": o.site_address or o.order_no or "s",
                          "coords": o.site_point.to_str()})
                synthesised += 1
            else:
                failed += 1
    resolved = (sum(1 for w in st.workers if w.home_point)
                    + sum(1 for o in st.orders if o.site_point))
    return resolved, failed, synthesised


async def _geocode_auto(st: State) -> int:
    n = 0
    for w in st.workers:
        if w.home_point is None:
            w.home_point = _synth_coords(w.home_address or w.name or "d")
            st.fallbacks.append({"kind": "worker", "key": w.id,
                     "address": w.home_address or w.name or "d",
                     "coords": w.home_point.to_str()})
            n += 1
    for o in st.orders:
        if o.site_point is None:
            o.site_point = _synth_coords(o.site_address or o.order_no or "s")
            st.fallbacks.append({"kind": "order", "key": o.order_no,
                      "address": o.site_address or o.order_no or "s",
                      "coords": o.site_point.to_str()})
            n += 1
    return n


def print_geocode_cache_summary(cache, stats: dict,
                               fallbacks: "list | None" = None) -> None:
    """"End-of-phase cache status summary for the geocode step.

            Tallies per-address outcomes in *stats* (hit / miss / saved /
            failed) so the user can track how populated the geocode cache is
            across runs. Mirrors the direction-matrix scan summary.
            """
    hits   = stats.get("hit", 0)
    miss   = stats.get("miss", 0)
    saved  = stats.get("saved", 0)
    failed = stats.get("failed", 0)
    scanned = hits + miss
    pct = (100 * hits / scanned) if scanned else 0.0
    loc = getattr(cache, "_path", None)
    locstr = f"sqlite:{loc}" if loc else "db"
    lines = [
        f"              [cache] geocode  summary           [{locstr}]",
        f"                addresses scanned          : {scanned}",
        f"                HIT     (from cache)        : {hits:<8d} ({pct:5.1f}%)",
        f"                MISS    (-> AMap API)       : {miss:<8d} "
                            f"({100 - pct:5.1f}%)",
        f"                 saved to cache             : {saved}",
        f"                 failed / empty             : {failed}",
     ]
    _seen: set = set()
    _fb = [f for f in (fallbacks or []) if f.get("key") not in _seen
             and not _seen.add(f.get("key"))]
    _n_worker = sum(1 for f in _fb if f.get("kind") == "worker")
    _n_order   = sum(1 for f in _fb if f.get("kind") == "order")
    lines += [
        f"        FALLBACK(local, NOT AMap)  : {len(_fb):<8d} "
            f"(workers={_n_worker}, orders={_n_order})",
     ]
    for f in _fb:
        lines.append(
            f"        - [{f.get('kind')}] {f.get('key')}   "
                f"addr={f.get('address')!r}  -> ({f.get('coords')})")
    print("\n".join(lines), flush=True)


async def step_geocode(st: State, city: str, mode: str, out: Path,
                       cache: "LocalCache | None" = None) -> None:
    # replay cross-run failure summary
    if len(st.failures) > 0:
        st.failures.print_replay_summary()
    have_key = bool(os.environ.get("AMAP_API_KEY"))
    n = total_failed = 0
    cache_stats: dict = {}
    if not have_key:
        n = await _geocode_auto(st)
        st.coords_source = "synthetic"
        print(f"[4/7] geocode        source=synthetic  city={city}                  "
                      f"(no AMAP_API_KEY -> deterministic pseudo-geo within 上海 bbox)")
    elif mode == "amap":
        n, total_failed, synthesised = await _geocode_amap(
                    st, city, fallback=False, cache=cache, stats=cache_stats)
        st.coords_source = "amap"
    else:                                                     # auto: AMap first, synth misses
        n, total_failed, synthesised = await _geocode_amap(
                    st, city, fallback=True, cache=cache, stats=cache_stats)
        st.coords_source = "amap+synth"
        if synthesised:
            print(f"                (synthesised coords for {synthesised} "
                               "missed/errored address, fallback)")
    if cache is not None:
        print_geocode_cache_summary(cache, cache_stats, fallbacks=st.fallbacks)
    else:
        # no cache backing: still surface the AMap-vs-fallback split so the
        # user sees which coordinates are local/synthetic, not from AMap.
        if st.fallbacks:
            n_w = sum(1 for f in st.fallbacks if f["kind"] == "worker")
            n_o = sum(1 for f in st.fallbacks if f["kind"] == "order")
            print(f"                 FALLBACK (local, NOT AMap): "
                              f"{len(st.fallbacks)} (workers={n_w}, orders={n_o})")
            for f in st.fallbacks:
                print(f"                   - [{f['kind']}] {f['key']} "
                        f"addr={f['address']!r} -> ({f['coords']})")
    print(f"               {n} points resolved, {total_failed} failed               "
                  f"(workers={len(st.workers)}, tasks={len(st.orders)})")
    _save(out, "geo", st)

# -- step 5: matrix ---------------------------------------------------------
async def step_matrix(st: State, mode: str, out: Path,
                      concurrency: int = 3, max_retries: int = 5,
                      cache: "LocalCache | None" = None) -> None:
    pts = [o.site_point for o in st.orders] + [w.home_point for w in st.workers]
    refs = [f"o:{o.order_no}" for o in st.orders] + [f"w:{w.id}" for w in st.workers]
    have_key = bool(os.environ.get("AMAP_API_KEY"))
    if mode == "amap" and not have_key:
        raise SystemExit("ERROR: matrix(amap) needs AMAP_API_KEY "
                              "(or pass --matrix euclidean / auto).")
    n_pts = len(st.orders) + len(st.workers)
    n_pairs = n_pts * (n_pts - 1) // 2
    print(f"[5/7] matrix         N={n_pts} ({len(st.orders)} tasks + {len(st.workers)} workers)        "
          f"{n_pairs} pairwise legs   mode={mode}")
    if len(st.failures) > 0:
        st.failures.print_replay_summary("matrix")
    if mode == "euclidean" or (mode == "auto" and not have_key):
        st.matrix = TravelMatrix.euclidean(pts, refs)
        st.matrix_source = "euclidean" if mode == "euclidean" else "euclidean(no key)"
    else:
         # shared httpx session — avoids thousands of new TLS handshakes.
         # No httpx-level retries: AmapClient does its own backoff-retry, the
         # right tool for rate-limiting; fast compounded retries only make CUQPS
         # worse.
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as session:
            client = AmapClient(api_key=os.environ.get("AMAP_API_KEY", ""),
                               session=session, max_retries=max_retries)
            # "lng,lat" endpoints from the local synthetic fallback, so the
            # scan summary can flag which nodes are NOT backed by real AMap.
            synth_set = {f["coords"] for f in st.fallbacks}
            st.failures.print_replay_summary()
            st.matrix = await build_travel_matrix(
                pts, refs, db=None, client=client,
                mode="driving", symmetric=True,
                concurrency=concurrency, local_cache=cache,
                synthetic_coords=synth_set or None,
                tracker=st.failures,
                )
        st.matrix_source = st.matrix.source
    m = st.matrix
    print(f"            done: {m.n} nodes, source={st.matrix_source}")
    _save(out, "matrix", st)


# -- step 6: solve ----------------------------------------------------------
def step_solve(st: State, args, out: Path) -> None:
    day_start, day_end = hhmm_seconds(args.day_start), hhmm_seconds(args.day_end)
    for w in st.workers:
        w.available_start = day_start
        w.available_end = day_end
        if args.max_orders is not None:
            w.max_orders = args.max_orders
    for o in st.orders:                       # every task is droppable (partial VRP)
        s = int(round(o.service_hours * 3600))
        o.drop_penalty = (args.penalty_base if args.drop_by == "count"
                          else args.penalty_base + args.per_hour_k * s)
        o.optional = True
    engine = "ortools"
    res = solve_dispatch(st.workers, st.orders, st.matrix,
                         timeout_s=args.timeout_s, force_optional=True)
    engine = (res.metadata or {}).get("note") or "ortools"
    st.result = res
    assigned = sum(len(r.assignments) for r in res.routes)
    dropped = len(res.unassigned_orders)
    total_svc = sum(o.service_hours for o in st.orders)
    cap = len(st.workers) * (day_end - day_start) / 3600.0
    print(f"[6/7] solve          engine={engine}  status={res.status.value}")
    print(f"       within {args.day_start}-{args.day_end}: "
          f" {assigned}/{len(st.orders)} tasks scheduled, "
          f" {dropped} dropped   "
          f"(penalty {'∝ task time' if args.drop_by == 'time' else '∝ count'})")
    print(f"       total on-site service = {total_svc:.1f} h  vs capacity {cap:.0f} h  "
          f"({total_svc/max(cap,1):.1f}x)   |  travel_obj={res.objective_value:.0f}s  "
          f"solve_time={res.solve_time_s}s")
    for r in res.routes:
        if r.assignments:
            lo, hi = r.assignments[0].arrival_s, r.assignments[-1].departure_s
            span = f"{hms(lo)}\u2192{hms(hi)}"
        else:
            span = "(idle)"
        print(f"         {r.worker_name:<8} {len(r.assignments)} stops    {span}")
    _save(out, "result", st)


# -- steps 1-3 and 7 --------------------------------------------------------
def step_hours(args, out: Path) -> State:
    lookup, unknown = load_working_hours(args.hours)
    st = State(lookup=lookup, unknown=unknown)
    print(f"[1/7] parse-hours    {args.hours}")
    print(f"       {len(lookup)} product heads -> hours    | "
          f"{len(unknown)} row(s) with unknown hours "
          f"(a task using them is DROPped, never 0):")
    for u in unknown[:6]:
        print(f"          - {u}")
    _save(out, "lookup", st)
    return st


def step_workers(args, out: Path, st: State) -> State:
    st.workers = import_workers(args.workers)
    modes = Counter(transport_name(w) for w in st.workers)
    print(f"[2/7] parse-workers  {args.workers}")
    print(f"       {len(st.workers)} workers    | by transport: {dict(modes)}")
    print(f"       (08:00-18:00 window applied at solve; "
          f"max_orders = unbounded unless --max-orders given)")
    _save(out, "workers", st)
    return st


def step_tasks(args, out: Path, st: State) -> State:
    orders, removed, qty = import_tasks(args.tasks, st.lookup, warn_qty=args.warn_qty)
    st.orders, st.removed, st.qty_warnings = orders, removed, qty
    total = sum(o.service_hours for o in orders)
    print(f"[3/7] parse-tasks    {args.tasks}    (+ product->hours lookup)")
    print(f"       KEPT {len(orders)} tasks   |  REMOVED {len(removed)}  "
          f"({dict(Counter(t.reason for t in removed))})")
    print(f"       total on-site service of KEPT = {total:.1f} h   "
          f"(avg {total/max(1,len(orders)):.2f} h/task)")
    print(f"       quantity warnings (N>={args.warn_qty}, flag-only): {len(qty)}")
    for q in qty:
        tag = "NO MATCH -> task dropped" if q["matched"] is None else f"-> {q['matched']}"
        print(f"          ord{q['order_no']}: qty={q['parsed_qty']}   {tag}")
    _save(out, "tasks", st)
    return st


def step_export(args, out: Path, st: State) -> Path:
    p = export_result(args.out, st.result, workers=st.workers, orders=st.orders,
                     removed=st.removed or None, qty_warnings=st.qty_warnings or None)
    print(f"[7/7] export        -> {p}")
    print(f"       sheets: 结果  汇总  未分配({len(st.result.unassigned_orders)})  "
          f"移除({len(st.removed)})  数量警告({len(st.qty_warnings)})")
    return p


# -- orchestration ----------------------------------------------------------
def run(args, out: Path, cache: "LocalCache | None" = None) -> Path:
    """Chain all seven steps in order, threading in-memory state + checkpoints."""
    st = State()
    st = step_hours(args, out)
    st = step_workers(args, out, st)
    st = step_tasks(args, out, st)
    asyncio.run(step_geocode(st, args.city, args.coords, out, cache=cache))
    asyncio.run(step_matrix(st, args.matrix, out, args.concurrency,
                           args.max_retries, cache=cache))
    step_solve(st, args, out)
    return step_export(args, out, st)


# -- single-step dispatch ---------------------------------------------------
# Each sub-command loads the JSON checkpoints from --workdir and runs its own
# step once. Pure-from-xlsx steps (hours/workers/tasks) rebuild their inputs;
# the API/solve steps require the prior checkpoint (run 'run' first).

def _ensure_lookup(args, out, st: State) -> State:
    if not st.lookup:
        st = step_hours(args, out)
    return st


def cmd_hours(args, out: Path, cache=None) -> int:
    step_hours(args, out)
    return 0


def cmd_workers(args, out: Path, cache=None) -> int:
    st = _load(out) if out.exists() else State()
    step_workers(args, out, st)
    return 0


def cmd_tasks(args, out: Path, cache=None) -> int:
    st = _load(out) if out.exists() else State()
    st = _ensure_lookup(args, out, st)        # hours lookup is the only input
    step_tasks(args, out, st)                # run exactly once
    return 0


async def _cmd_geocode(args, out: Path, cache: 'LocalCache | None' = None) -> int:
    st = _load(out) if out.exists() else State()
    st = _ensure_lookup(args, out, st)
    if not st.orders:                          # ensure tasks exist, don't re-run below
        st = step_tasks(args, out, st)
    await step_geocode(st, args.city, args.coords, out, cache=cache)
    return 0


async def _cmd_matrix(args, out: Path, cache: 'LocalCache | None' = None) -> int:
    st = _load(out) if out.exists() else State()
    if not st.workers or not st.orders:
        raise SystemExit("ERROR: no geocoded workers/orders in --workdir; "
                          "run 'geocode' (or 'run') first.")
    await step_matrix(st, args.matrix, out, args.concurrency,
                      args.max_retries, cache=cache)
    return 0


def cmd_solve(args, out: Path, cache=None) -> int:
    st = _load(out) if out.exists() else State()
    if st.matrix is None:
        raise SystemExit("ERROR: no travel matrix in --workdir; "
                          "run 'matrix' (or 'run') first.")
    step_solve(st, args, out)
    return 0


def cmd_export(args, out: Path, cache=None) -> int:
    st = _load(out) if out.exists() else State()
    if st.result is None:
        raise SystemExit("ERROR: no solve result in --workdir; "
                          "run 'solve' (or 'run') first.")
    step_export(args, out, st)
    return 0


def cmd_run(args, out: Path, cache: "LocalCache | None" = None) -> int:
    run(args, out, cache)
    return 0

_COMMANDS = {
      "hours": cmd_hours, "workers": cmd_workers, "tasks": cmd_tasks,
      "geocode": _cmd_geocode, "matrix": _cmd_matrix,
      "solve": cmd_solve, "export": cmd_export, "run": cmd_run,
}
_ASYNC = {"geocode", "matrix"}


# -- argparse ---------------------------------------------------------------
def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("-w", "--workers", default="docs/workers.xlsx")
    sp.add_argument("-t", "--tasks", default="docs/tasks.xlsx")
    sp.add_argument("-H", "--hours", default="docs/working_hours.xlsx")
    sp.add_argument("--city", default=DEFAULT_CITY)
    sp.add_argument("--out", default="result.xlsx")
    sp.add_argument("--workdir", default="./_run",
                    help="dir for per-step JSON checkpoints (verify / re-run one step)")
    sp.add_argument("--day-start", default=DEFAULT_DAY_START)
    sp.add_argument("--day-end", default=DEFAULT_DAY_END)
    sp.add_argument("--max-orders", type=int, default=None,
                    help="per-worker cap; default = unbounded (18:00 binds)")
    sp.add_argument("--timeout-s", type=float, default=None,
                    help="OR-Tools wall-clock budget; default = run until done")
    sp.add_argument("--warn-qty", type=int, default=DEFAULT_WARN_QTY,
                    help="flag 商品 lines whose parsed quantity >= this (decision #7)")
    sp.add_argument("--drop-by", choices=["time", "count"], default="time",
                    help="drop-penalty mode (time -> penalty ∝ task service time)")
    sp.add_argument("--penalty-base", type=float, default=DEFAULT_PENALTY_BASE)
    sp.add_argument("--per-hour-k", type=float, default=DEFAULT_PER_HOUR_K)
    sp.add_argument("--coords", choices=["amap", "auto"], default="auto",
                    help="coord source: amap=require key; auto=synthetic if no key")
    sp.add_argument("--matrix", choices=["amap", "euclidean", "auto"], default="auto",
                    help="travel-matrix source: auto -> amap if key else euclidean")
    sp.add_argument("--concurrency", type=int, default=3,
                    help="AMap API calls in flight at once (lower avoids CUQPS throttle)")
    sp.add_argument("--max-retries", type=int, default=5,
                    help="retries per AMap call on transient / rate-limit responses")
    sp.add_argument("--cache", default=None,
                    help="SQLite file to persist/reuse AMap geocode + travel "
                         "results across runs (skip to bypass caching)")
    sp.add_argument("--no-cache", action="store_true",
                    help="disable any --cache even if set")
    sp.add_argument("--clean-failures", action="store_true",
                    help="wipe api_failures.json; after fixing root cause")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Single-day VRPTW-with-drop dispatch pipeline "
                      "(pure data transform + AMap + OR-Tools; no DB / cache).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    _desc = {
          "hours": "step 1: working_hours.xlsx -> product->hours lookup",
          "workers": "step 2: workers.xlsx -> Worker[]",
          "tasks": "step 3: tasks.xlsx + hours -> kept orders (+removed/qty)",
          "geocode": "step 4: fill coordinates (AMap or synthetic)",
          "matrix": "step 5: build pairwise travel matrix (AMap/euclidean)",
          "solve": "step 6: OR-Tools VRPTW-with-drop -> DispatchResult",
          "export": "step 7: write result.xlsx (+移除/+数量警告)",
          "run": "run all seven steps end-to-end",
      }
    for name in ("hours", "workers", "tasks", "geocode", "matrix", "solve", "export", "run"):
        sp = sub.add_parser(name, help=_desc[name])
        _add_common(sp)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = build_parser()
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    out = Path(args.workdir).absolute()

    # --clean-failures: wipe api_failures.json before loading
    if getattr(args, "clean_failures", False):
        import shutil
        fp = out / "api_failures.json"
        if fp.exists():
            shutil.unlink(fp)
            print(f"[clean-failures] wiped {fp}", flush=True)
        else:
            print(f"[clean-failures] no api_failures.json to wipe", flush=True)

    # ── open SQLite cache if requested ──
    cache_path = args.cache if not args.no_cache else None
    cache = LocalCache(cache_path) if cache_path else None
    if cache is not None:
        stats = cache._stats_sync()
        print(f"[cache] SQLite cache opened at {cache_path}  "
              f"(geocode={stats['geocode']} entries, "
              f"travel={stats['travel']} entries)")
    try:
        fn = _COMMANDS[args.cmd]
        if args.cmd in _ASYNC:
            return asyncio.run(fn(args, out, cache))
        return fn(args, out, cache)
    finally:
        if cache is not None:
            cache.close()
            print(f"[cache] SQLite cache closed ({cache_path})")


if __name__ == "__main__":
    raise SystemExit(main())


