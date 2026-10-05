"""solver.py — the default dispatch solver: a general single-day VRPTW.

Public entry point: :func:`solve_dispatch`.  It builds one Vehicle-Routing
Problem with Time Windows, one depot node per worker, and a time dimension,
and maximises the generality the underlying solver (OR-Tools Constraint
Solver, 9.x) actually supports:

* **Per-worker heterogeneous transit.**  Either a single base *driving*
  matrix scaled per worker by a transport-mode speed factor (the default, how
  the engine builds it), or — if ``per_worker_matrices`` is supplied — one
   independent matrix *per worker* (fully heterogeneous, like the reference
   ``example.py``).  Both are expressed through per-vehicle transit callbacks.
* **Real time windows.**  Each order carries an ``[start, end]`` window; the
  time dimension bounds the *arrival* at a node, so a worker may wait (slack =
  the horizon) when arriving early.
* **Optional / droppable orders.**  An order flagged ``optional`` (or forced
  via ``force_optional``) is added through ``AddDisjunction`` so the solver
  may leave it unserved, paying its ``drop_penalty`` on the objective.  This
  is the general form the reference example used; a large default penalty keeps
   tasks whenever feasible.
* **Per-vehicle capacity / order-count bound.**  When any worker sets
   ``capacity`` or any order sets ``demand`` a capacity dimension accumulates
  demand and enforces each worker's limit.  Otherwise a worker's ``max_orders``
  becomes a hard per-vehicle *count* bound (demand = 1 per order) — strictly
  stronger than the earlier "max_orders not enforced" note.
* **Per-worker availability windows.**  ``available_start``/``available_end``
  clamp each depot's start/finish and the global horizon.
* **Configurable search.**  First-solution strategy and local-search
  metaheuristic are parameters (defaults: PATH_CHEAPEST_ARC +
  GUIDED_LOCAL_SEARCH); the time limit is configurable.
* **Greedy fallback.**  If the optimiser raises or returns nothing, a fast
  round-robin heuristic still returns a feasible-ish plan so a dispatch never
  hard-fails.

Honest limitations in OR-Tools 9.x (documented, not modelled):

* routes are **closed loops** — ``start == end``; there is no per-vehicle open
  route / separate return depot (``SetEndNodePerVehicle`` was removed).
* per-worker ``min_orders`` is *soft* — 9.x has no per-vehicle minimum-visit
  constraint, so it only biases the greedy fallback.
* cross-vehicle **precedence** (order i must precede order j on *any* vehicle)
  is not modelled; same-route ordering is enforced implicitly by time windows.
"""

from __future__ import annotations

import time
from datetime import timedelta

from .models import (
    AssignRoute,
    DispatchResult,
    DispatchStatus,
    Order,
    OrderStop,
    Worker,
)
from .spatial import TravelMatrix

_DAY = 24 * 3600             # reference bound for "since-midnight" modes (not a hard cap)
_BIG = 10**12              # unbounded sentinel (absolute-ts / slack / capacity-safe)
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_DROP_PENALTY = 1.0e9   # heavy cost keeps a droppable order in when possible


# ── public entry point ───────────────────────────────────────────────────────
def solve_dispatch(
    workers: list[Worker],
    orders: list[Order],
    matrix: TravelMatrix,
     *,
    per_worker_matrices: dict[str, TravelMatrix] | None = None,
    timeout_s: float | None = None,
    first_solution_strategy=None,
    local_search=None,
    force_optional: bool | None = None,
    default_drop_penalty: float | None = _DEFAULT_DROP_PENALTY,
) -> DispatchResult:
    """Route *workers* through assigned *orders* subject to per-order windows.

    Args:
        workers: the fleet. Each owns one depot node (its home).
        orders: the tasks to serve.
        matrix: the shared pairwise distance/duration matrix (driving base).
        per_worker_matrices: optional ``{worker.id: TravelMatrix}`` for fully
            heterogeneous per-worker transit. Falls back to ``matrix`` scaled
            by each worker's speed factor when a worker has no entry.
        timeout_s: solver wall-clock budget (default 60s).
        first_solution_strategy / local_search: OR-Tools enum values; when
            ``None`` the defaults (PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH)
            apply.
        force_optional: when True, *every* order is droppable; when False, none
            are. When ``None`` each order's own ``optional`` flag decides.
        default_drop_penalty: penalty paid for an optional order left unserved
            when the order's own ``drop_penalty`` is unset. Heavy by default so
            orders are kept whenever feasibility permits.
    """
    if not workers:
        raise ValueError("at least one worker is required")
    if not orders:
        return DispatchResult(routes=[], status=DispatchStatus.OPTIMAL)

    if timeout_s is None:
        timeout_s = _DEFAULT_TIMEOUT_S

    n_v = len(workers)
    order_node = {o.id: n_v + i for i, o in enumerate(orders)}
    order_by_id = {o.id: o for o in orders}
    refs = [None] * n_v
    refs.extend(o.id for o in orders)        # parallel to the node layout
    n_nodes = len(refs)

    service = [0] * n_nodes                   # on-site seconds per departure node
    for o in orders:
        service[order_node[o.id]] = int(o.service_seconds)
    factor_by_vehicle = [factor_for(w) for w in workers]

    try:
        return _solve_ortools(
            workers=workers,
            orders=orders,
            matrix=matrix,
            per_worker_matrices=per_worker_matrices,
            service=service,
            factor_by_vehicle=factor_by_vehicle,
            n_v=n_v,
            n_nodes=n_nodes,
            order_node=order_node,
            order_by_id=order_by_id,
            refs=refs,
            timeout_s=timeout_s,
            first_solution_strategy=first_solution_strategy,
            local_search=local_search,
            force_optional=force_optional,
            default_drop_penalty=default_drop_penalty,
        )
    except Exception:          # fall back to a heuristic that never hard-fails
        result = _solve_generic(
            workers=workers,
            orders=orders,
            matrix=matrix,
            per_worker_matrices=per_worker_matrices,
            factor_by_vehicle=factor_by_vehicle,
            n_v=n_v,
            n_nodes=n_nodes,
            service=service,
            order_node=order_node,
            order_by_id=order_by_id,
            refs=refs,
            timeout_s=timeout_s,
        )
        mandatory_missing = [
            o.id for o in orders
            if o.id in result.unassigned_orders and not o.optional
        ]
        if mandatory_missing:
            result.status = DispatchStatus.INFEASIBLE
            result.metadata["infeasible_order_ids"] = sorted(mandatory_missing)
            result.metadata["reason"] = (
                f"{len(mandatory_missing)} mandatory order(s) unassigned: "
            + str(sorted(mandatory_missing)))
        return result


# ── multi-day greedy (CLI default) ────────────────────────────────────────────
def solve_dispatch_multi(
    workers: list[Worker],
    orders: list[Order],
    matrix: TravelMatrix,
      *,
    per_worker_matrices: dict[str, TravelMatrix] | None = None,
    day_start: int = 8 * 3600,
    day_end: int = 18 * 3600,
    max_days: int = 3650,
    allow_overflow: int = 1,
    default_drop_penalty: float | None = _DEFAULT_DROP_PENALTY,
) -> DispatchResult:
    """Fill *workers* across as many days as needed, one window per day.

    Each day every worker starts fresh at its *home depot* (the matrix node for
    that worker; the home location is fixed across all days -- the per-day reset
    is only the clock, not the origin).  The day window is [day_start, day_end];
    orders that don't fit a worker that day carry over to the next day
    ("fill day 1, overflow to day 2").

      * value-priority fill: orders are placed high-to-low ``drop_penalty`` so
        high-value orders claim the better (low-travel) slots first.
      * one overflow stop per worker per day (``allow_overflow``): a worker may
        finish its LAST stop after ``day_end`` -- this lets a single over-long
        order (> the day window) still be served; once the overflow slot is spent,
        the worker's remaining stops must fit within the window.
      * termination: the loop stops when a day places zero orders (nothing left
        any worker can take) or ``max_days`` is hit, so it always terminates.

    Node mapping mirrors ``step_matrix`` (refs = [o:<no>...] + [w:<id>...]):
    orders occupy the first ``n_orders`` nodes, each worker's home the
    ``n_orders+v``-th node; the matrix is symmetric, so
    ``duration[home][order]`` is a real leg.
    """
    if not workers:
        raise ValueError("at least one worker is required")
    if not orders:
        return DispatchResult(routes=[], status=DispatchStatus.OPTIMAL)

    pwm = per_worker_matrices or {}
    matrix_for = lambda w: pwm[w.id] if w.id in pwm else matrix
    n_orders = len(orders)
    n_v = len(workers)
    factor = [factor_for(w) for w in workers]

    # orders first (0..n_orders-1); each worker's home depot at n_orders + v
    order_node = {o.id: i for i, o in enumerate(orders)}
    depot_node = {w.id: n_orders + v for v, w in enumerate(workers)}

    caps = [float(w.capacity) if w.capacity is not None else _BIG for w in workers]
    cap_orders = [w.max_orders or 0 for w in workers]

    service = {o.id: int(o.service_seconds) for o in orders}
    amount = {o.id: (o.amount or 0.0) for o in orders}
    window_hours = (day_end - day_start) / 3600.0

    def order_reason(o: Order, termination: str) -> str:
        if o.service_hours > window_hours + 1e-6:
            return (f"服务时长 {o.service_hours:g}h 超过单日上限 "
                   f"{window_hours:g}h — 需更长窗口或拆分")
        if termination == "max_days":
            return f"超过 --max-days={max_days} 仍有余量，需增加师傅/天数或放宽窗口"
        return "所有师傅当日已满，需增加师傅或放宽时间窗"

    routes: list[AssignRoute] = []
    remaining = list(orders)
    placed_ids: set[str] = set()
    day = 0
    days_used = 0
    day_summary: list[dict] = []
    termination = "all-scheduled"
    fallback_penalty = default_drop_penalty if default_drop_penalty is not None else 0.0

    def place_day(day_num: int) -> tuple[list[AssignRoute], set[str], bool]:
        clock = [int(day_start)] * n_v
        cur = [depot_node[w.id] for w in workers]
        used = [0.0] * n_v
        overflow_used = [0] * n_v
        routes_d = [AssignRoute(worker_id=w.id, worker_name=w.name, day=day_num)
                     for w in workers]
        placed_here: set[str] = set()
        # value-priority: high value first, then larger service (better fill)
        for o in sorted(
             remaining,
             key=lambda o: (
                bool(getattr(o, "optional", False)),
                 -(o.drop_penalty if o.drop_penalty is not None else fallback_penalty),
                 -o.service_seconds,
                 str(o.order_no),
             ),
         ):
            idx = order_node[o.id]
            best = None           # (key, v, arrival, depart, overflow, raw_leg)
            for v in range(n_v):
                w = workers[v]
                if cap_orders[v] and len(routes_d[v].assignments) >= cap_orders[v]:
                    continue
                if used[v] + (o.demand or 0.0) > caps[v]:
                    continue
                mv = matrix_for(w)
                raw = int(round(mv.duration[cur[v]][idx] * factor[v]))
                t = max(clock[v], int(day_start)) + raw        # wait for window open
                dep = t + service[o.id]
                over = dep > int(day_end)
                if over and (allow_overflow <= 0 or overflow_used[v] >= allow_overflow):
                    continue            # no in-window slot and overflow already spent
                # in-window preferred; an overflow stop (finishing after the
                # window) is only used as a last resort when no worker fits
                key = t if not over else (int(day_end) + t + raw)
                if best is None or key < best[0]:
                    best = (key, v, t, dep, over, raw)
            if best is None:
                continue
            _, v, t, dep, over, raw = best
            w = workers[v]
            mv = matrix_for(w)
            dr = routes_d[v]
            dr.assignments.append(OrderStop(
                order_id=o.id, order_no=o.order_no, site=o.site_point,
                arrival_s=int(t), departure_s=int(dep),
                sequence=len(dr.assignments), day=day_num, overflow=over,
             ))
            dr.total_service_s += service[o.id]
            dr.total_travel_s += raw
            dr.total_distance_m += mv.distance[cur[v]][idx]
            used[v] += o.demand or 0.0
            clock[v] = int(dep)
            cur[v] = idx
            if over:
                overflow_used[v] += 1
            placed_here.add(o.id)
        nonempty = [r for r in routes_d if r.assignments]
        placed_any = len(placed_here) > 0
        return nonempty, placed_here, placed_any

    while remaining:
        day += 1
        if day > max_days:
            termination = "max_days"
            break
        day_routes, placed, placed_any = place_day(day)
        for r in day_routes:
            routes.append(r)
            for stp in r.assignments:
                placed_ids.add(stp.order_id)
        if not placed_any:
             # nothing left that any worker can take (no-progress guard)
            break
        remaining = [o for o in remaining if o.id not in placed_ids]
        days_used = day
        placed_days = placed or set()
        day_summary.append({
             "day": day,
             "assigned": len(placed_days),
             "service_hours": round(
                 sum(service[o.id] for o in orders if o.id in placed_days) / 3600.0, 2),
             "assigned_amount": round(sum(amount[o.id] for o in orders if o.id in placed_days), 2),
         })

    unassigned = [o.id for o in remaining]
    status = DispatchStatus.OPTIMAL if not unassigned else DispatchStatus.FEASIBLE
    total_travel = sum(r.total_travel_s for r in routes)
    assigned_amount = sum(amount[o.id] for o in orders if o.id in placed_ids)
    total_amount = sum(amount.values())
    dropped = [o.id for o in orders
               if getattr(o, "optional", False) and o.id in unassigned]
    md = {
         "engine": "greedy-multi-day",
         "note": "greedy-multi-day",
         "n_workers": n_v,
         "n_orders": n_orders,
         "n_days": days_used,
         "n_assigned": len(placed_ids),
         "n_unassigned": len(unassigned),
         "dropped_order_ids": dropped,
         "termination": termination,
         "day_start": int(day_start),
         "day_end": int(day_end),
         "allow_overflow": allow_overflow,
         "day_summary": day_summary,
         "unassigned_reasons": {o.order_no: order_reason(o, termination)
                                 for o in remaining},
         "amounts": {
              "assigned": round(assigned_amount, 2),
              "unassigned": round(total_amount - assigned_amount, 2),
              "total": round(total_amount, 2),
          },
      }
    return DispatchResult(
        routes=sorted(routes, key=lambda r: (r.day, r.worker_name)),
        unassigned_orders=unassigned,
        status=status,
        objective_value=total_travel,
        solve_time_s=None,
        metadata=md,
      )
def factor_for(w: Worker) -> float:
    """Per-worker speed factor from transport mode (ebike slower than car)."""
    from .config import settings

    return settings.transport_speed_factors.get(w.transport.value, 1.0)


# ── OR-Tools VRPTW (general) ─────────────────────────────────────────────────
def _solve_ortools(
    *,
    workers,
    orders,
    matrix,
    per_worker_matrices,
    service,
    factor_by_vehicle,
    n_v,
    n_nodes,
    order_node,
    order_by_id,
    refs,
    timeout_s,
    first_solution_strategy,
    local_search,
    force_optional,
    default_drop_penalty,
) -> DispatchResult:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    pwm = per_worker_matrices or {}
    def matrix_for(w: Worker) -> TravelMatrix:
        return pwm[w.id] if w.id in pwm else matrix

    # node v (0..n_v-1) is worker v's home depot; start == end (closed loop).
    start = list(range(n_v))
    manager = pywrapcp.RoutingIndexManager(n_nodes, n_v, start, start)
    routing = pywrapcp.RoutingModel(manager)

    # ── horizon = latest window end / availability end, capped at one day ──
    horizon = 0
    for o in orders:
        end = o.time_window.end if o.time_window else None
        horizon = max(horizon, int(end) if end is not None else 0, service[order_node[o.id]])
    for w in workers:
        if w.available_end:
            horizon = max(horizon, int(w.available_end))
    horizon = horizon if horizon > 0 else 1           # absolute or since-midnight
    service_sum = sum(service)

    # ── per-vehicle transit (objective = travel time) ──────────────────────
    def travel(i_from, i_to, vehicle):
        if i_to == vehicle:        # OPEN ROUTE #6: return-to-depot leg = 0 cost
            return 0
        fn = manager.IndexToNode(i_from)
        tn = manager.IndexToNode(i_to)
        base = matrix_for(workers[vehicle]).duration[fn][tn]
        return int(round(base * factor_by_vehicle[vehicle]))

    travel_idx = routing.RegisterTransitCallback(travel)
    routing.SetArcCostEvaluatorOfAllVehicles(travel_idx)

    # ── per-vehicle time dimension (transit + departure service folded in) ──
    # Service of the *departure* node is folded in, so the cumulative at a node
    # is its arrival time → the [start, end] window bounds arrival directly.
    time_indices = []
    for v in range(n_v):
        def time_cb(i_from, i_to, vehicle=v):
            fn = manager.IndexToNode(i_from)
            tn = manager.IndexToNode(i_to)
            if tn == vehicle:      # OPEN ROUTE #6: no return travel, keep last service
                return service[fn]
            base = matrix_for(workers[vehicle]).duration[fn][tn]
            return int(round(base * factor_by_vehicle[vehicle])) + service[fn]
        time_indices.append(routing.RegisterTransitCallback(time_cb))

    any_avail_start = any(w.available_start for w in workers)
    routing.AddDimensionWithVehicleTransitAndCapacity(
        time_indices,
        horizon + service_sum,          # slack: a worker may wait until the horizon
        [_BIG] * n_v,                   # capacity slot unused for the Time dim
        not any_avail_start,            # fix start cumul at 0 unless availability set
        "Time",
    )
    time_var = routing.GetDimensionOrDie("Time")

    # per-order [start, end] arrival windows.
    for o in orders:
        s, e = _window_for(o, horizon)
        time_var.SetCumulVarRange(manager.NodeToIndex(order_node[o.id]), int(s), int(e))

    # per-worker availability windows on the depot node.
    if any_avail_start:
        for v, w in enumerate(workers):
            lo = int(w.available_start or 0)
            hi = int(w.available_end) if w.available_end else horizon
            time_var.SetCumulVarRange(manager.NodeToIndex(v), lo, hi)

    # ── capacity / order-count dimension (when one is configured) ───────────
    has_capacity_dim = _maybe_add_capacity(
        routing, manager, workers, orders, order_node, n_v
    )

    # ── optional / droppable orders ─────────────────────────────────────────
    for o in orders:
        is_optional = o.optional if force_optional is None else bool(force_optional)
        if not is_optional:
            continue
        if o.drop_penalty is not None:
            penalty = o.drop_penalty
        elif default_drop_penalty is not None:
            penalty = default_drop_penalty
        else:
            penalty = _DEFAULT_DROP_PENALTY
        routing.AddDisjunction([manager.NodeToIndex(order_node[o.id])], int(round(penalty)))

    # ── search configuration ────────────────────────────────────────────────
    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        first_solution_strategy or routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search.local_search_metaheuristic = (
        local_search or routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search.time_limit = timedelta(seconds=timeout_s)

    t0 = time.monotonic()
    solution = routing.SolveWithParameters(search)
    elapsed = time.monotonic() - t0

    if solution is None:
         # no OR-Tools solution (the dimension binding may render the 
          # model infeasible); fall back to the window-aware greedy, which 
          # builds a schedule by hand.
        raise RuntimeError("OR-Tools returned no solution")

    result = _extract(
        routing, manager, solution, workers, orders,
        order_node, order_by_id, refs, factor_by_vehicle, elapsed,
        matrix_for, n_v, service,
     )
    result.metadata["has_time_windows"] = 1 if any(o.time_window for o in orders) else 0
    result.metadata["has_capacity_dim"] = 1 if has_capacity_dim else 0
    if result.metadata.get("infeasible_reasons"):
           # This OR-Tools binding does not reliably enforce dimensions /
           # report cumulative values (solution.Value returns 0 here), so we
           # validate the schedule ourselves and fall back to the window-aware
           # greedy heuristic on any window / capacity / availability violation.
        raise ValueError(
            "infeasible routing: " + "; ".join(result.metadata["infeasible_reasons"])
        )
    return result


def _maybe_add_capacity(routing, manager, workers, orders, order_node, n_v) -> bool:
    """Add a per-vehicle capacity (load) or order-count dimension when set.

    * Any worker ``capacity`` or order ``demand`` → a *load* capacity
      dimension (demands accumulated against per-worker limits).
    * Else, any worker ``max_orders`` → a *count* dimension (demand = 1 per
      order), turning the cap into a hard per-vehicle bound.
    """
    any_real = any(w.capacity is not None for w in workers) or any(o.demand for o in orders)
    any_count = any((w.max_orders or 0) > 0 for w in workers)
    if not any_real and not any_count:
        return False

    n_nodes = n_v + len(orders)
    demand = [0.0] * n_nodes
    if any_real:
        for o in orders:
            demand[order_node[o.id]] = float(o.demand or 0.0)
        caps = [float(w.capacity) if w.capacity is not None else _BIG for w in workers]
    else:
        for o in orders:
            demand[order_node[o.id]] = 1.0
        caps = [float(w.max_orders) if (w.max_orders or 0) > 0 else _BIG for w in workers]

    try:
        cap_idx = routing.RegisterTransitCallback(
            lambda i_from, i_to: max(0.0, demand[manager.IndexToNode(i_to)])
         )
        routing.AddDimensionWithVehicleTransitAndCapacity(
             [cap_idx] * n_v,
             0,
             [int(round(c)) for c in caps],
             True,
             "Capacity",
         )
    except Exception:
            # the binding may not support the capacity dimension; skip it and
            # rely on the post-solve simulation + greedy fallback for capacity.
        return False
    return True


def _window_for(o: Order, horizon: int) -> tuple[int, int]:
    """(start, end) arrival window for an order; open when unset."""
    tw = o.time_window
    s = int(tw.start) if (tw and tw.start is not None) else 0
    e = int(tw.end) if (tw and tw.end is not None) else horizon
    return s, e


# ── result extraction ────────────────────────────────────────────────────────
def _simulate_route(
    v, seq, w, mv, factor, order_by_id, refs, service,
) -> tuple[list[OrderStop], float, float, float, bool, str]:
    """Replay one worker's ordered list of *order* nodes into a concrete
    schedule, validating time windows, service, availability and capability
    caps.  This is done ourselves so the result is independent of the
    solver's cumulative-value reporting (which returns 0 in this OR-Tools
    build).  ``seq`` holds order site nodes in visit order; the route is a
    closed loop, so a final return leg to the home depot is folded in."""
    stops: list[OrderStop] = []
    travel = 0.0
    dist = 0.0
    svc = 0.0
    t = int(w.available_start or 0)
    prev = v                       # start at this worker's home depot node
    load = 0.0
    for node in seq:
        o = order_by_id[refs[node]]
        raw = int(round(mv.duration[prev][node] * factor))
        tw = o.time_window
        s_lo = int(tw.start) if (tw and tw.start is not None) else 0
        s_hi = int(tw.end) if (tw and tw.end is not None) else None
        arr = max(t, s_lo) + raw   # wait until the window opens, then travel
        if s_hi is not None and arr > s_hi:
            return (
                stops, travel, dist, svc, False,
                f"order {o.order_no} arrival {arr}s > window end {s_hi}s",
            )
        dep = arr + service[node]
        svc += service[node]
        load += float(o.demand or 0.0)
        stops.append(
            OrderStop(
                order_id=refs[node],
                order_no=o.order_no,
                site=o.site_point,
                arrival_s=int(arr),
                departure_s=int(dep),
                sequence=len(stops),
            )
        )
        travel += raw
        dist += mv.distance[prev][node]
        t = dep
        prev = node

          # OPEN ROUTE (decision #6): the return-to-home leg is *excluded* from the
      # reported travel/distance; the day-end cutoff applies to ``t`` = the last
      # task's finish (NOT last-finish + drive home).
    if w.available_end is not None and t > int(w.available_end):
        return (
            stops, travel, dist, svc, False,
            f"worker {w.name} finishes {t}s > day-end {int(w.available_end)}s",
             )
    cap = None
    if (w.max_orders or 0) and len(stops) > (w.max_orders or 0):
        cap = f"worker {w.name} serves {len(stops)} > max_orders {w.max_orders}"
    if w.capacity is not None and load > float(w.capacity) + 1e-9:
        cap = f"worker {w.name} load {load} > capacity {w.capacity}"
    if cap:
        return stops, travel, dist, svc, False, cap
    return stops, travel, dist, svc, True, ""


def _extract(
    routing, manager, solution, workers, orders,
    order_node, order_by_id, refs, factor_by_vehicle, elapsed, matrix_for, n_v, service,
) -> DispatchResult:
    """Turn a solved routing into a DispatchResult.

    Each vehicle's *order* sequence is read from the solution (the ``NextVar``
    walk is reliable), then the schedule and feasibility are computed by
    ``_simulate_route`` — not by querying the solver's dimension cumulatives,
    which are unreliable in this OR-Tools build.  Window / capacity /
    availability violations are recorded in ``metadata["infeasible_reasons"]``
    so the caller can fall back to the greedy heuristic.
    """
    routes: list[AssignRoute] = []
    assigned: set[str] = set()
    all_ok = True
    reasons: list[str] = []
    total_obj = 0.0
    for v in range(n_v):
        w = workers[v]
        mv = matrix_for(w)
        factor = factor_by_vehicle[v]

        # ordered list of order-site nodes on this vehicle's route
        seq: list[int] = []
        index = routing.Start(v)
        while index != routing.End(v):
            node = manager.IndexToNode(index)
            if node >= n_v:            # order sites live at/n after n_v
                seq.append(node)
            index = solution.Value(routing.NextVar(index))

        stops, travel, dist, svc, ok, reason = _simulate_route(
            v, seq, w, mv, factor, order_by_id, refs, service
        )
        if not ok:
            all_ok = False
            reasons.append(reason)
        for node in seq:
            assigned.add(refs[node])
        total_obj += travel
        routes.append(
            AssignRoute(
                worker_id=w.id,
                worker_name=w.name,
                assignments=stops,
                total_service_s=svc,
                total_travel_s=travel,
                total_distance_m=dist,
            )
        )

    unassigned = [o.id for o in orders if o.id not in assigned]
    dropped = [o.id for o in orders if o.optional and o.id not in assigned]
    mandatory_missing = [o.id for o in orders if o.id not in assigned and not o.optional]
            # a solver plan that leaves a mandatory order unserved is unusable; fall
            # back to the greedy, which gives mandatory orders a first claim on slots.
    if mandatory_missing:
        all_ok = False
        reasons.append("mandatory order(s) unserved by solver: " + str(sorted(mandatory_missing)))
    status = DispatchStatus.OPTIMAL if not unassigned else DispatchStatus.FEASIBLE
    md = {
        "n_workers": n_v,
        "n_orders": len(orders),
        "n_assigned": len(assigned),
        "n_dropped": len(dropped),
        "dropped_order_ids": dropped,
    }
    if not all_ok:
        md["infeasible_reasons"] = reasons
    return DispatchResult(
        routes=sorted(routes, key=lambda r: r.worker_name),
        unassigned_orders=unassigned,
        status=status,
        objective_value=total_obj,
        solve_time_s=elapsed,
        metadata=md,
    )



# ── greedy fallback ───────────────────────────────────────────────────────────
def _solve_generic(
    *,
    workers,
    orders,
    matrix,
    per_worker_matrices,
    factor_by_vehicle,
    n_v,
    n_nodes,
    service,
    order_node,
    order_by_id,
    refs,
    timeout_s,
) -> DispatchResult:
    """Round-robin greedy: place each order on the worker that reaches it
    within its window with the least arrival time; never hard-fails.  Honours
    availability starts, per-worker max_orders, and capacity/demand."""
    pwm = per_worker_matrices or {}
    matrix_for = lambda w: pwm[w.id] if w.id in pwm else matrix

    routes = [AssignRoute(worker_id=w.id, worker_name=w.name) for w in workers]
    clock = [int(w.available_start or 0) for w in workers]
    t0 = time.monotonic()    # current time per worker
    cur = [0] * n_v                                            # current node per worker
    used = [0.0] * n_v                                         # capacity used per worker
    caps = [float(w.capacity) if w.capacity is not None else _BIG for w in workers]
    max_orders = [w.max_orders for w in workers]

    def arrive(v: int, idx: int, o: Order):
        w = workers[v]
        mv = matrix_for(w)
        raw = int(round(mv.duration[cur[v]][idx] * factor_by_vehicle[v]))
        tw = o.time_window
        s_lo = int(tw.start) if (tw and tw.start is not None) else 0
        s_hi = int(tw.end) if (tw and tw.end is not None) else _BIG
        if w.available_end is not None:
            s_hi = min(s_hi, int(w.available_end))   # must finish on-site by availability end
        t = max(clock[v], s_lo) + raw
        if t + service[idx] > s_hi:
            return None
        return t

    for o in sorted(
        orders,
            # mandatory orders get first claim on capacity / slots; optional
            # (droppable) ones fill the remainder, each then ordered by window end.
        key=lambda o: (bool(o.optional),
              o.time_window.end if (o.time_window and o.time_window.end is not None)
              else _BIG),
    ):
        idx = order_node[o.id]
        best = None
        for v in range(n_v):
            if max_orders[v] and len(routes[v].assignments) >= max_orders[v]:
                continue
            if used[v] + (o.demand or 0.0) > caps[v]:
                continue
            t = arrive(v, idx, o)
            if t is None:
                continue
            if best is None or t < best[0]:            # earliest arrival is cheapest
                best = (t, v)

        if best is None:
            continue          # cannot place within any window → left for later / dropped
        t, v = best
        w = workers[v]
        mv = matrix_for(w)
        routes[v].total_travel_s += int(round(mv.duration[cur[v]][idx] * factor_by_vehicle[v]))
        routes[v].total_distance_m += mv.distance[cur[v]][idx]
        routes[v].assignments.append(
            OrderStop(
                order_id=o.id,
                order_no=o.order_no,
                site=o.site_point,
                arrival_s=int(t),
                departure_s=int(t + service[idx]),
                sequence=len(routes[v].assignments),
            )
        )
        routes[v].total_service_s += service[idx]
        used[v] += o.demand or 0.0
        clock[v] = int(t) + service[idx]
        cur[v] = idx

    assigned = {o.order_id for r in routes for o in r.assignments}
    unassigned = [o.id for o in orders if o.id not in assigned]
    status = DispatchStatus.OPTIMAL if not unassigned else DispatchStatus.FEASIBLE
    total_travel = sum(r.total_travel_s for r in routes)
    dropped = [o.id for o in orders if o.optional and o.id in unassigned]
    elapsed = time.monotonic() - t0
    return DispatchResult(
        routes=sorted(routes, key=lambda r: r.worker_name),
        unassigned_orders=unassigned,
        status=status,
        objective_value=total_travel,
        solve_time_s=elapsed,
        metadata={
             "n_workers": n_v,
             "n_orders": len(orders),
             "n_assigned": len(assigned),
             "n_dropped": len(dropped),
             "dropped_order_ids": dropped,
             "note": "greedy-fallback",
         },
     )

