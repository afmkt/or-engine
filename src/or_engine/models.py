"""Domain models — the concrete input/output shapes for this stage.

This replaces the earlier polymorphic ``domain`` package (Problem /
RoutingProblem / SchedulingProblem / …). There is a single real problem:
*dispatch a set of workers to a set of orders on one day, routing each
worker through assigned order sites subject to per-order time windows.*
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── coordinate primitive ───────────────────────────────────────────────────
class Point2D(BaseModel):
    """Geographic coordinate.  Amap returns 'lng,lat'; this wraps that.

    ``wkt()`` yields ``POINT(lng lat)`` for PostGIS (SRID 4326, lon-lat).
    """

    lng: float
    lat: float

    @classmethod
    def from_str(cls, s: str) -> Point2D:
        parts = s.split(",")
        if len(parts) != 2:
            raise ValueError(f"Invalid coordinate string: {s!r}")
        return cls(lng=float(parts[0]), lat=float(parts[1]))

    @classmethod
    def from_tuple(cls, pair: tuple[float, float]) -> Point2D:
        return cls(lng=float(pair[0]), lat=float(pair[1]))

    def to_str(self) -> str:
        return f"{self.lng},{self.lat}"

    def to_tuple(self) -> tuple[float, float]:
        return (self.lng, self.lat)

    def wkt(self) -> str:
        return f"POINT({self.lng} {self.lat})"

    @classmethod
    def coerce(cls, v: Any) -> Point2D | None:
        if v is None:
            return None
        if isinstance(v, str):
            return cls.from_str(v) if v.strip() else None
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return cls.from_tuple(tuple(v))
        if isinstance(v, Point2D):
            return v
        raise TypeError(f"Cannot convert {type(v).__name__} to Point2D")


# ── enumerations ────────────────────────────────────────────────────────────
class TransportMode(StrEnum):
    """Worker's transport — affects travel-time computation."""

    EBIKE = "ebike"
    CAR_SH = "car_sh"  # car, Shanghai plate
    CAR_OUT = "car_out"  # car, out-of-town plate (city driving restrictions)

    @classmethod
    def coerce(cls, v: str | None) -> "TransportMode":
        if not v:
            return cls.CAR_SH
        key = str(v).strip().lower().replace("-", "").replace("_", "")
        for m in cls:
            if m.value == key or m.value.replace("_", "") == key:
                return m
        # tolerate the raw Chinese/loose labels from the workbook
        if any(t in str(v) for t in ("电", "bik", "bike", "e-bike", "ebike")):
            return cls.EBIKE
        if any(t in str(v) for t in ("外", "out")):
            return cls.CAR_OUT
        return cls.CAR_SH


class TimeWindow(BaseModel):
    """A service window, as seconds since 00:00:00.

    Either bound may be ``None`` (open).  Build from 'HH:MM' with
    :meth:`from_hhmm`.
    """

    start: int | None = None
    end: int | None = None

    @classmethod
    def from_hhmm(cls, start: str | None, end: str | None) -> "TimeWindow":
        def p(s: str | None) -> int | None:
            if not s:
                return None
            h, m = s.split(":")
            return int(h) * 3600 + int(m) * 60

        return cls(start=p(start), end=p(end))

    @classmethod
    def coerce(cls, v: Any) -> "TimeWindow | None":
        if v is None:
            return None
        if isinstance(v, TimeWindow):
            return v
        if isinstance(v, str) and "-" in v:
            a, b = v.split("-")
            return cls.from_hhmm(a.strip() or None, b.strip() or None)
        if isinstance(v, dict):
            return cls(**v)
        return None


# ── inputs ─────────────────────────────────────────────────────────────────
class Worker(BaseModel):
    """安装师傅 (installation worker)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    home_address: str | None = None
    home_point: Point2D | None = None  # geocoded from home_address
    transport: TransportMode = TransportMode.CAR_SH
    available_start: int | None = None  # earliest on-the-road, s/since-midnight
    available_end: int | None = None  # must finish by, s/since-midnight
    max_orders: int = 20  # daily cap on assigned orders
    min_orders: int = 0            # soft: preferred minimum served (not a hard 9.x constraint)
    capacity: float | None = None    # hard per-worker capacity (units); None = unbounded
    phone: str | None = None


class Order(BaseModel):
    """订单 (work order) — a service site with a time window."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid4()))
    order_no: str = Field(default_factory=lambda: f"O-{uuid4().hex[:8]}")
    order_type: str | None = None  # 安装 / 维修 / 售后
    merchant: str | None = None
    site_address: str | None = None
    site_point: Point2D | None = None  # geocoded from site_address
    product: str | None = None           # 商品 — raw multi-line item list
    contact: str | None = None           # 联系人 — usually "name/phone"
    date: str | None = None  # YYYY-MM-DD, or None = undated
    time_window: TimeWindow | None = None   # when work should start/finish
    service_hours: float = 0.0   # 做单时长 (hours on-site); resolved from 商品 via working_hours
    quantity: int = 1
    amount: float | None = None
    note: str | None = None
    optional: bool = False             # may be left unserved (droppable at a penalty)
    drop_penalty: float | None = None    # objective penalty incurred when this order is dropped
    demand: float = 0.0              # capacity demand (units); 0 = none

    @property
    def service_seconds(self) -> int:
        return int(self.service_hours * 3600)


class WorkingHour(BaseModel):
    """Product -> on-site labour-hours lookup row (工作工时表).

    One row per product description from working_hours.xlsx
      (商品 -> 工时（小时）). Used to price a task's on-site service time
    without carrying the raw hours on the task itself.
"""

    model_config = ConfigDict(extra="ignore")

    product: str      # 商品 — natural key, e.g. "两移门"
    hours: float = 0.0      # 工时（小时）
    note: str | None = None
# ── output ──────────────────────────────────────────────────────────────────
class OrderStop(BaseModel):
    """One visit in a worker's route."""

    order_id: str
    order_no: str
    site: Point2D
    arrival_s: int  # seconds since midnight
    departure_s: int  # arrival + service time
    sequence: int


class AssignRoute(BaseModel):
    """One worker's ordered sequence of assigned orders."""

    worker_id: str
    worker_name: str
    assignments: list[OrderStop] = Field(default_factory=list)
    total_service_s: float = 0.0
    total_travel_s: float = 0.0
    total_distance_m: float = 0.0


class DispatchStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    ERROR = "error"


class DispatchResult(BaseModel):
    """The full output of a dispatch run (→ Excel export)."""

    routes: list[AssignRoute] = Field(default_factory=list)
    unassigned_orders: list[str] = Field(default_factory=list)
    status: DispatchStatus = DispatchStatus.OPTIMAL
    objective_value: float | None = None
    solve_time_s: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
