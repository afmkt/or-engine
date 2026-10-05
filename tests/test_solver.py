"""Regression tests for the dispatch solver.

These lock in the fixes for the three critical bugs:
  1. the OR-Tools path must actually run (not silently fall back to greedy),
  2. solve_time_s must always be populated,
  3. the cached travel-matrix builder must return a complete matrix even when
     every pair is served from cache (the old early-`return` left it partial).
"""

from __future__ import annotations

import asyncio
import unittest

from or_engine.models import Order, Point2D, TimeWindow, Worker
from or_engine.solver import solve_dispatch, solve_dispatch_multi
from or_engine.spatial.travel import build_travel_matrix, TravelMatrix


# ── fixtures ─────────────────────────────────────────────────────────────
def _workers() -> list[Worker]:
    return [
        Worker(id="w1", name="Zhang", transport="car_sh",
               home_point=Point2D(lng=121.50, lat=31.24), max_orders=3),
        Worker(id="w2", name="Li", transport="ebike",
               home_point=Point2D(lng=121.45, lat=31.21), max_orders=3),
    ]


def _orders() -> list[Order]:
    return [
        Order(id="o1", order_no="A001",
              site_point=Point2D(lng=121.49, lat=31.24),
              time_window=TimeWindow.from_hhmm("08:30", "18:00")),
        Order(id="o2", order_no="A002",
              site_point=Point2D(lng=121.44, lat=31.22),
              time_window=TimeWindow.from_hhmm("09:00", "17:00")),
        Order(id="o3", order_no="A003",
              site_point=Point2D(lng=121.56, lat=31.19),
              time_window=TimeWindow.from_hhmm("10:00", "18:00")),
    ]


def _matrix() -> "TravelMatrix":
    ws, os_ = _workers(), _orders()
    refs = [f"home:{w.id}" for w in ws] + [o.id for o in os_]
    pts = [w.home_point for w in ws] + [o.site_point for o in os_]
    return TravelMatrix.euclidean(pts, refs)


# ── fakes ────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, distance_m, duration_s):
        self.ok = True
        self.distance_m = distance_m
        self.duration_s = duration_s


class _FakeClient:
    """Direction calls: deterministic distance = 1000m, duration = 60s."""
    async def direction(self, origin, destination, mode="driving"):
        return _FakeResp(1000.0, 60.0)


class _FakeDB:
    """In-memory travel-time cache stand-in."""
    def __init__(self) -> None:
        self.cache: dict[tuple[str, str, str], dict] = {}
        self.saves: list[dict] = []

    async def get_travel_pair(self, from_ref, to_ref, mode):
        row = self.cache.get((from_ref, to_ref, mode))
        return dict(row) if row else None

    async def save_travel_pair(self, *, from_ref, to_ref, mode,
                                distance_m, duration_s):
        self.cache[(from_ref, to_ref, mode)] = {
            "distance_m": distance_m, "duration_s": duration_s,
        }
        self.saves.append({
            "from_ref": from_ref, "to_ref": to_ref, "mode": mode,
            "distance_m": distance_m, "duration_s": duration_s,
        })


# ── tests ────────────────────────────────────────────────────────────────
class TestSolverRuns(unittest.TestCase):
    def test_ortools_path_runs(self):
        ws, os_ = _workers(), _orders()
        result = solve_dispatch(ws, os_, _matrix(), timeout_s=5.0)

        # the optimiser must produce the result — not the greedy fallback
        self.assertNotEqual(result.metadata.get("note"), "greedy-fallback")
        self.assertIsNotNone(result.solve_time_s, "solve_time_s must be set")
        self.assertGreaterEqual(result.solve_time_s, 0.0)
        # every order assigned
        self.assertEqual(result.unassigned_orders, [])

    def test_metadata_dimension_flags(self):
        ws, os_ = _workers(), _orders()
        result = solve_dispatch(ws, os_, _matrix(), timeout_s=5.0)
        # orders carry time windows → has_time_windows must be 1 (used to
        # raise NameError before the fix)
        self.assertEqual(result.metadata.get("has_time_windows"), 1)
        self.assertIn(result.metadata.get("has_capacity_dim"), (0, 1))


class TestGreedyFallback(unittest.TestCase):
    def test_fallback_sets_solve_time(self):
        # force the fallback by handing the solver a deliberately broken matrix
        ws = _workers()
        broken = _matrix()
        broken.duration = [[0.0] * broken.n for _ in range(broken.n)]
        result = solve_dispatch(ws, _orders(), broken, timeout_s=1.0)
        # whatever path produced it, solve_time_s must be non-None
        self.assertIsNotNone(result.solve_time_s)


class TestTravelMatrixCache(unittest.TestCase):
    def _pts_and_refs(self):
        ws, os_ = _workers(), _orders()
        refs = [f"home:{w.id}" for w in ws] + [o.id for o in os_]
        pts = [w.home_point for w in ws] + [o.site_point for o in os_]
        return pts, refs

    def test_compute_and_writeback_complete_matrix(self):
        pts, refs = self._pts_and_refs()
        db = _FakeDB()
        m = asyncio.run(build_travel_matrix(
            pts, refs, client=_FakeClient(), db=db,
            mode="driving", symmetric=True, use_cache=True,
        ))
        self.assertIsInstance(m, TravelMatrix)
        # every distinct off-diagonal pair must be filled, not just the first
        n = len(pts)
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                self.assertGreater(m.distance[i][j], 0.0,
                         f"distance[{i}][{j}] unfilled")
                self.assertGreater(m.duration[i][j], 0.0,
                         f"duration[{i}][{j}] unfilled")
        # symmetric fill present
        self.assertEqual(m.distance[0][2], m.distance[2][0])

    def test_all_cache_hit_still_returns_matrix(self):
        pts, refs = self._pts_and_refs()
        db = _FakeDB()
        # first build populates the cache
        asyncio.run(build_travel_matrix(
            pts, refs, client=_FakeClient(), db=db,
            symmetric=True, use_cache=True,
        ))
        self.assertTrue(db.saves, "first call should have written to cache")
        # second build is entirely cache-served (todo empty) — the old early
        # `return` inside the loop returned None when nothing was left to compute
        m = asyncio.run(build_travel_matrix(
            pts, refs, client=_FakeClient(), db=db,
            symmetric=True, use_cache=True,
        ))
        self.assertIsInstance(m, TravelMatrix, "cache-only build returned None")
        n = len(pts)
        for i in range(n):
            for j in range(n):
                if i != j:
                    self.assertGreater(m.duration[i][j], 0.0)


class TestMultiDaySolver(unittest.TestCase):
    """Lock in the new multi-day (as many days as needed) dispatch behaviour."""

    def _matrix(self, n_orders, n_workers, dur=0.5):
        refs = [f"o{k+1}" for k in range(n_orders)] + [f"w{k+1}" for k in range(n_workers)]
        n = len(refs)
        dist = [[0.0 if i == j else 1000.0 for j in range(n)] for i in range(n)]
        dur_mat = [[0.0 if i == j else dur for j in range(n)] for i in range(n)]
        return TravelMatrix(node_refs=refs, distance=dist, duration=dur_mat, source="test")

    def test_spills_to_multiple_days(self):
        w = [Worker(id="w1", name="W", transport="car_sh",
                    home_point=Point2D(lng=121.5, lat=31.24), max_orders=10)]
        orders = [
            Order(id=f"o{k}", order_no=f"A{k:03d}",
                site_point=Point2D(lng=121.5 + 0.01 * k, lat=31.24 + 0.01 * k),
                service_hours=3.0, amount=100.0 * k) for k in range(1, 7)
        ]
        for o in orders:
            o.time_window = TimeWindow.from_hhmm("08:00", "18:00")
        m = self._matrix(6, 1)
        res = solve_dispatch_multi(
            w, orders, m, day_start=8 * 3600, day_end=18 * 3600,
            default_drop_penalty=1e9,
        )
        self.assertEqual(len(res.unassigned_orders), 0, "all 18h must be placed")
        self.assertGreaterEqual((res.metadata or {}).get("n_days", 0), 2)
        self.assertEqual((res.metadata or {}).get("termination", ""), "all-scheduled")
        total_amt = (res.metadata or {}).get("amounts", {}).get("assigned", 0.0)
        self.assertAlmostEqual(total_amt, 2100.0)
        stops = list(sum((r.assignments for r in res.routes), []))
        self.assertTrue(all(hasattr(st, "day") for st in stops))
        per_day = {}
        for r in res.routes:
            for st in r.assignments:
                per_day[st.day or r.day] = per_day.get(st.day or r.day, 0) + int(st.overflow)
        for d, n in per_day.items():
            self.assertLessEqual(n, 1, f"more than 1 overflow on day {d}")

    def test_no_progress_stops(self):
        w = [Worker(id="w1", name="W", transport="car_sh",
                    home_point=Point2D(lng=121.5, lat=31.24))]
        orders = [Order(id="o1", order_no="A001",
                        site_point=Point2D(lng=121.5, lat=31.24),
                        service_hours=100.0, amount=1.0)]
        orders[0].time_window = TimeWindow.from_hhmm("08:00", "09:00")
        m = self._matrix(1, 1)
        res = solve_dispatch_multi(
            w, orders, m, day_start=8 * 3600, day_end=9 * 3600,
            max_days=50, allow_overflow=1, default_drop_penalty=1e9,
        )
        self.assertGreaterEqual(sum(len(r.assignments) for r in res.routes), 1)
        self.assertLessEqual((res.metadata or {}).get("n_days", 0), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
