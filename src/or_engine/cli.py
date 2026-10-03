"""Standalone single-day dispatch pipeline (pure data transformation, no DB/cache).

A demonstration / CLI entry point that runs the *whole* one-day VRPTW-with-drop
pipeline with **no database and no cache** -- only file transform, an AMap
geocode + driving-matrix call, and the OR-Tools optimiser:

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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .excel import export_result, import_workers
from .models import DispatchResult, Order, Point2D, Worker
from .spatial.amap import AmapClient
from .spatial.travel import TravelMatrix, build_travel_matrix
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
        w("meta", {"coords_source": st.coords_source})
    elif step == "matrix" and st.matrix is not None:
        m = st.matrix
        w("matrix", {"node_refs": m.node_refs, "distance": m.distance,
                       "duration": m.duration, "source": m.source})
    elif step == "result" and st.result is not None:
        w("result", st.result.model_dump(mode="json"))


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
    return st


# -- step 4: geocode --------------------------------------------------------
def _synth_coords(addr: str) -> Point2D:
    """Deterministic pseudo-geocode into the 上海 bounding box (offline fallback)."""
    h = hash(addr or "?") % 100000
    lat = _SH_LAT[0] + (h % 10000) / 10000.0 * (_SH_LAT[1] - _SH_LAT[0])
    lng = _SH_LNG[0] + (h // 10000) / 1000.0 * (_SH_LNG[1] - _SH_LNG[0])
    return Point2D(lat=round(lat, 6), lng=round(lng, 6))


async def _geocode_amap(st: State, city: str) -> tuple[int, int]:
    client = AmapClient(api_key=os.environ.get("AMAP_API_KEY", ""))
    resolved = failed = 0
    for w in st.workers:
        if w.home_point is None and w.home_address:
            hits = await client.geocode(w.home_address, city=city)
            if hits:
                w.home_point = hits[0].location
                resolved += 1
            else:
                failed += 1
    for o in st.orders:
        if o.site_point is None and o.site_address:
            hits = await client.geocode(o.site_address, city=city)
            if hits:
                o.site_point = hits[0].location
                resolved += 1
            else:
                failed += 1
    return resolved, failed


async def _geocode_auto(st: State) -> int:
    n = 0
    for w in st.workers:
        if w.home_point is None:
            w.home_point = _synth_coords(w.home_address or w.name or "d")
            n += 1
    for o in st.orders:
        if o.site_point is None:
            o.site_point = _synth_coords(o.site_address or o.order_no or "s")
            n += 1
    return n


async def step_geocode(st: State, city: str, mode: str, out: Path) -> None:
    have_key = bool(os.environ.get("AMAP_API_KEY"))
    n = total_failed = 0
    if not have_key:
        n = await _geocode_auto(st)
        st.coords_source = "synthetic"
        print(f"[4/7] geocode        source=synthetic  city={city}    "
              f"(no AMAP_API_KEY -> deterministic pseudo-geo within 上海 bbox)")
    elif mode == "amap":
        resolved, failed = await _geocode_amap(st, city)
        st.coords_source, n, total_failed = "amap", resolved, failed
        print(f"[4/7] geocode        source=amap  city={city}")
    else:
        resolved, failed = await _geocode_auto(st)
        st.coords_source, n, total_failed = "amap+synth", resolved, failed
        print(f"[4/7] geocode        source=amap+synth  city={city}")
    print(f"               {n} points resolved, {total_failed} failed    "
          f"(workers={len(st.workers)}, tasks={len(st.orders)})")
    _save(out, "geo", st)

# -- step 5: matrix ---------------------------------------------------------
async def step_matrix(st: State, mode: str, out: Path) -> None:
    pts = [o.site_point for o in st.orders] + [w.home_point for w in st.workers]
    refs = [f"o:{o.order_no}" for o in st.orders] + [f"w:{w.id}" for w in st.workers]
    have_key = bool(os.environ.get("AMAP_API_KEY"))
    if mode == "amap" and not have_key:
        raise SystemExit("ERROR: matrix(amap) needs AMAP_API_KEY "
                          "(or pass --matrix euclidean / auto).")
    if mode == "euclidean" or (mode == "auto" and not have_key):
        st.matrix = TravelMatrix.euclidean(pts, refs)
        st.matrix_source = "euclidean" if mode == "euclidean" else "euclidean(no key)"
    else:
        client = AmapClient(api_key=os.environ.get("AMAP_API_KEY", ""))
        st.matrix = await build_travel_matrix(pts, refs, db=None, client=client,
                                             mode="driving", symmetric=True)
        st.matrix_source = st.matrix.source
    m = st.matrix
    pairs = m.n * (m.n - 1) // 2
    print(f"[5/7] matrix         source={st.matrix_source}  N={m.n} "
          f"({len(st.orders)} tasks + {len(st.workers)} workers)")
    print(f"         {pairs} pairwise legs computed via {st.matrix_source}")
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
def run(args, out: Path) -> Path:
    """Chain all seven steps in order, threading in-memory state + checkpoints."""
    st = State()
    st = step_hours(args, out)
    st = step_workers(args, out, st)
    st = step_tasks(args, out, st)
    asyncio.run(step_geocode(st, args.city, args.coords, out))
    asyncio.run(step_matrix(st, args.matrix, out))
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


def cmd_hours(args, out: Path) -> int:
    step_hours(args, out)
    return 0


def cmd_workers(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    step_workers(args, out, st)
    return 0


def cmd_tasks(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    st = _ensure_lookup(args, out, st)        # hours lookup is the only input
    step_tasks(args, out, st)                # run exactly once
    return 0


async def _cmd_geocode(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    st = _ensure_lookup(args, out, st)
    if not st.orders:                          # ensure tasks exist, don't re-run below
        st = step_tasks(args, out, st)
    await step_geocode(st, args.city, args.coords, out)
    return 0


async def _cmd_matrix(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    if not st.workers or not st.orders:
        raise SystemExit("ERROR: no geocoded workers/orders in --workdir; "
                          "run 'geocode' (or 'run') first.")
    await step_matrix(st, args.matrix, out)
    return 0


def cmd_solve(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    if st.matrix is None:
        raise SystemExit("ERROR: no travel matrix in --workdir; "
                          "run 'matrix' (or 'run') first.")
    step_solve(st, args, out)
    return 0


def cmd_export(args, out: Path) -> int:
    st = _load(out) if out.exists() else State()
    if st.result is None:
        raise SystemExit("ERROR: no solve result in --workdir; "
                          "run 'solve' (or 'run') first.")
    step_export(args, out, st)
    return 0


def cmd_run(args, out: Path) -> int:
    run(args, out)
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
    p = build_parser()
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    out = Path(args.workdir).absolute()
    fn = _COMMANDS[args.cmd]
    if args.cmd in _ASYNC:
        return asyncio.run(fn(args, out))
    return fn(args, out)


if __name__ == "__main__":
    raise SystemExit(main())
