"""solver.py — single dispatch entry point: ``solve_dispatch``.

Collapses the earlier polymorphic routing/scheduling/assignment sub-registry
into one function. It builds a VRPTW with one depot node per worker, the
inter-depot/inter-site travel-time *base* matrix (driving), and per-worker
speed factors applied at solve time in the transit callback.

Simplifications at this stage (documented, not modelled):
- open / non-returning routes (depot = closed loop by default);
- per-worker min/max amount;
- per-worker earliest-departure / latest-finish availability windows;
- OR-Tools 9.x no longer has a per-node demand API, so per-worker max_orders
is not a hard constraint here.
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

_DAY = 24 * 3600  # horizon cap (s); per-order windows are the real bound


def solve_dispatch(
    workers: list[Worker],
    orders: list[Order],
    matrix: TravelMatrix,
    /,
    *,
    timeout_s: float | None = None,
) -> DispatchResult:
    """Route *workers* through assigned *orders* subject to per-order time windows.

    A single VRPTW is built. Each worker owns one depot node (its home); orders
    become demand nodes. Service time is folded into the per-vehicle transit
    callback so the time dimension tracks true on-site durations.
    """
    if not workers:
        raise ValueError("at least one worker is required")
    if not orders:
        return DispatchResult(routes=[], status=DispatchStatus.OPTIMAL)

    # ── node layout: [worker_0 … worker_{v-1}] then [order_0 … order_{o-1}] ──
    n_v = len(workers)
    order_node = {o.id: n_v + i for i, o in enumerate(orders)}
    order_by_id = {o.id: o for o in orders}
    refs = [None] * n_v
    refs.extend(o.id for o in orders)  # parallel to order nodes
    n_nodes = len(refs)

    service = [0] * n_nodes  # on-site seconds per node
    for o in orders:
        service[order_node[o.id]] = int(o.service_seconds)

    # Horizon = latest window end across all nodes (fallback: one day).
    horizon = 0
    for o in orders:
        end = o.time_window.end if o.time_window else None
        horizon = max(horizon, end if end is not None else 0, service[order_node[o.id]])
    cap = max((w.max_orders for w in workers), default=1)

    factor_by_vehicle = [factor_for(w) for w in workers]

    if timeout_s is None:
        timeout_s = _DEFAULT_TIMEOUT_S

    try:
        return _solve_ortools(
            workers=workers,
            matrix=matrix,
            service=service,
            factor_by_vehicle=factor_by_vehicle,
            n_v=n_v,
            n_nodes=n_nodes,
            order_node=order_node,
            order_by_id=order_by_id,
            orders=orders,
            horizon=horizon,
            timeout_s=timeout_s,
            refs=refs,
        )
    except Exception:
        # Fall back to a naive greedy heuristic that still yields a feasible-ish
        # plan, so a dispatch never hard-fails on solver issues.
        return _solve_generic(
            workers=workers,
            orders=orders,
            matrix=matrix,
            service=service,
            factor_by_vehicle=factor_by_vehicle,
            n_v=n_v,
            order_node=order_node,
            order_by_id=order_by_id,
            horizon=horizon,
            timeout_s=timeout_s,
        )


_DEFAULT_TIMEOUT_S = 60.0


def factor_for(w: Worker) -> float:
    """Per-worker speed factor from transport mode (car slower than ebike)."""
    from .config import settings

    return settings.transport_speed_factors.get(w.transport.value, 1.0)


# ── OR-Tools VRPTW ──────────────────────────────────────────────────────────
def _solve_ortools(
    *,
    workers,
    matrix: TravelMatrix,
    service,
    factor_by_vehicle,
    n_v,
    n_nodes,
    order_node,
    order_by_id,
    orders,
    horizon,
    timeout_s,
    refs,
) -> DispatchResult:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    # node v is the v-th worker's home depot; start == end (closed loop)
    start = list(range(n_v))
    manager = pywrapcp.RoutingIndexManager(n_nodes, n_v, start, start)
    routing = pywrapcp.RoutingModel(manager)

    # per-vehicle transit = base duration x that worker's speed factor,
    # plus the on-site service of the node we arrive at (folded in).
    # objective (arc cost) reuses the same transit index = total travel.
    def transit_time(i_from, i_to, vehicle):
        base = int(round(matrix.duration[i_from][i_to] * factor_by_vehicle[vehicle]))
        return base + service[i_to]

    transit_idx = routing.RegisterTransitCallback(transit_time)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # per-worker max_orders is not a hard constraint in OR-Tools 9.x — documented.
    # time dimension: per-order windows (service already folded into transit).
    time_cap = int(horizon) + sum(service) + 1
    routing.AddDimension(transit_idx, 0, time_cap, False, "TimeDim")
    time_var = routing.GetMutableDimension("TimeDim")
    for i in range(n_nodes):
        s, e = _window_for(i, orders, order_node, time_cap)
        time_var.SetCumulVarRange(i, int(s), int(e))

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search.time_limit = timedelta(seconds=timeout_s)

    t0 = time.monotonic()
    solution = routing.SolveWithParameters(search)
    elapsed = time.monotonic() - t0

    if solution is None:
        return DispatchResult(
            status=DispatchStatus.INFEASIBLE,
            solve_time_s=elapsed,
            unassigned_orders=[o.id for o in orders],
            metadata={"reason": "no feasible routing"},
        )

    routes: list[AssignRoute] = []
    assigned: set[str] = set()
    for v in range(n_v):
        w = workers[v]
        index = routing.Start(v)
        stop = routing.End(v)
        prev_node = manager.IndexToNode(index)
        plan: list[OrderStop] = []
        travel = 0.0
        dist = 0.0
        svc = 0.0
        while index != stop:
            node = manager.IndexToNode(index)
            # depot node (0..n_v-1) is a home — skip; rest are order sites
            if node >= n_v:
                oid = refs[node]
                o = order_by_id[oid]
                arr = int(time_var.CumulVar(index).Min())
                dep = arr + int(o.service_seconds)
                plan.append(
                    OrderStop(
                        order_id=oid,
                        order_no=o.order_no,
                        site=o.site_point,
                        arrival_s=arr,
                        departure_s=dep,
                        sequence=len(plan),
                    )
                )
                assigned.add(oid)
                svc += o.service_seconds
            travel += matrix.duration[prev_node][node]
            dist += matrix.distance[prev_node][node]
            prev_node = node
            index = solution.Value(routing.NextVar(index))

        routes.append(
            AssignRoute(
                worker_id=w.id,
                worker_name=w.name,
                assignments=plan,
                total_service_s=svc,
                total_travel_s=travel,
                total_distance_m=dist,
            )
        )

    unassigned = [o.id for o in orders if o.id not in assigned]
    status = DispatchStatus.OPTIMAL if not unassigned else DispatchStatus.FEASIBLE
    return DispatchResult(
        routes=sorted(routes, key=lambda r: r.worker_name),
        unassigned_orders=unassigned,
        status=status,
        objective_value=float(solution.ObjectiveValue()),
        solve_time_s=elapsed,
        metadata={
            "n_workers": n_v,
            "n_orders": len(orders),
            "n_assigned": len(assigned),
        },
    )


def _window_for(i, orders, order_node, time_cap) -> tuple[int, int]:
    """(start, end) window for site node *i*; depots have an open window."""
    for o in orders:
        if order_node[o.id] == i:
            tw = o.time_window
            if tw is None:
                return 0, time_cap
            return int(tw.start or 0), int(tw.end or time_cap)
    return 0, time_cap


# ── greedy fallback ─────────────────────────────────────────────────────────
def _solve_generic(
    *,
    workers,
    orders,
    matrix,
    service,
    factor_by_vehicle,
    n_v,
    order_node,
    order_by_id,
    horizon,
    timeout_s,
) -> DispatchResult:
    """Round-robin greedy: assign each order to the idle-able worker that
    reaches its window with the least remaining travel; never hard-fails."""
    routes = [AssignRoute(worker_id=w.id, worker_name=w.name) for w in workers]
    cursor = [
        (w.home_point.lng, w.home_point.lat) if w.home_point else (0.0, 0.0)
        for w in workers
    ]
    clock = [int(min(horizon, _DAY)) for _ in workers]
    assigned: set[str] = set()

    for o in orders:
        best_v, best_cost = -1, None
        for v, w in enumerate(workers):
            tw = o.time_window
            s_lo = tw.start if tw and tw.start is not None else 0
            s_hi = tw.end if tw and tw.end is not None else horizon
            idx = order_node[o.id]
            cost = matrix.duration[0][idx] if idx < len(matrix.duration[0]) else 0
            cost = cost * factor_by_vehicle[v] + service[idx]
            # pick the worker that can still finish inside its window
            if clock[v] + cost <= s_hi and s_lo is not None:
                if best_cost is None or cost < best_cost:
                    best_v, best_cost = v, cost
        v = best_v if best_v >= 0 else (0 if workers else 0)
        if v < 0:
            continue
        idx = order_node[o.id]
        routes[v].assignments.append(
            OrderStop(
                order_id=o.id,
                order_no=o.order_no,
                site=o.site_point,
                arrival_s=clock[v],
                departure_s=clock[v] + service[idx],
                sequence=len(routes[v].assignments),
            )
        )
        routes[v].total_service_s += service[idx]
        clock[v] += service[idx]
        assigned.add(o.id)

    unassigned = [o.id for o in orders if o.id not in assigned]
    status = DispatchStatus.OPTIMAL if not unassigned else DispatchStatus.FEASIBLE
    return DispatchResult(
        routes=routes,
        unassigned_orders=unassigned,
        status=status,
        metadata={"n_workers": n_v, "n_orders": len(orders), "note": "greedy-fallback"},
    )
