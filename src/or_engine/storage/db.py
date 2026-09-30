"""storage/db.py — the only persistence layer.

Replaces the earlier *job queue + event log + SQLAlchemy ORM*. There is no
async work queue here: a dispatch request runs synchronously to completion.
We keep just what the requirements ask for — a Postgres/PostGIS database
that

* stores workers and orders, and
* **caches Amap distance/duration results** in ``travel_times`` to avoid
re-calling a slow, rate-limited API.

Built on raw ``asyncpg`` (no ORM). The schema is created idempotently by
:meth:`DB.ensure_schema` at startup.
"""

from __future__ import annotations

import asyncpg

from ..models import Order, Point2D, TimeWindow, TransportMode, Worker

# ── schema ────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;

-- workers (安装师傅): one row per worker; geocoded home cached too.
CREATE TABLE IF NOT EXISTS workers (
id              TEXT PRIMARY KEY,
name            TEXT NOT NULL,
home_address    TEXT,
home_lng        DOUBLE PRECISION,
home_lat        DOUBLE PRECISION,
home_point      GEOGRAPHY(POINT, 4326),
transport       TEXT NOT NULL DEFAULT 'car_sh',
available_start INTEGER,
available_end   INTEGER,
max_orders      INTEGER NOT NULL DEFAULT 20,
phone           TEXT,
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workers_home ON workers USING GIST (home_point);

-- orders (订单): one row per work order; geocoded site cached too.
CREATE TABLE IF NOT EXISTS orders (
id            TEXT PRIMARY KEY,
order_no      TEXT NOT NULL,
order_type    TEXT,
merchant      TEXT,
site_address  TEXT,
site_lng      DOUBLE PRECISION,
site_lat      DOUBLE PRECISION,
site_point    GEOGRAPHY(POINT, 4326),
date          TEXT,
window_start  INTEGER,
window_end    INTEGER,
service_hours DOUBLE PRECISION DEFAULT 0,
quantity      INTEGER DEFAULT 1,
amount        DOUBLE PRECISION,
note          TEXT,
updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_site ON orders USING GIST (site_point);

-- Amap travel-time cache. geography has no btree UNIQUE opclass, so we
-- key on the text 'lng,lat' refs of both endpoints.
CREATE TABLE IF NOT EXISTS travel_times (
id         BIGSERIAL PRIMARY KEY,
from_ref   TEXT NOT NULL,
to_ref     TEXT NOT NULL,
from_point GEOGRAPHY(POINT, 4326),
to_point   GEOGRAPHY(POINT, 4326),
mode       TEXT NOT NULL DEFAULT 'driving',
distance_m DOUBLE PRECISION,
duration_s DOUBLE PRECISION,
cached_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
UNIQUE (from_ref, to_ref, mode)
);
CREATE INDEX IF NOT EXISTS idx_travel_from ON travel_times (from_ref);
CREATE INDEX IF NOT EXISTS idx_travel_to   ON travel_times (to_ref);
"""


class DB:
    """Async Postgres/PostGIS handle built on an asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls, database_url: str, min_size: int = 5, max_size: int = 10
    ) -> "DB":
        pool = await asyncpg.create_pool(
            database_url, min_size=min_size, max_size=max_size
        )
        db = cls(pool)
        await db.ensure_schema()
        return db

    async def ensure_schema(self) -> None:
        """Idempotently apply the schema (runs on every startup)."""
        async with self.pool.acquire() as conn:
            await conn.execute(_SCHEMA)

    # ── travel-time cache ───────────────────────────────────────────────
    async def get_travel_pair(
        self, from_ref: str, to_ref: str, mode: str
    ) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT distance_m, duration_s FROM travel_times "
                "WHERE from_ref=$1 AND to_ref=$2 AND mode=$3",
                from_ref,
                to_ref,
                mode,
            )
        return dict(row) if row else None

    async def save_travel_pair(
        self,
        *,
        from_ref: str,
        to_ref: str,
        mode: str,
        distance_m: float,
        duration_s: float,
    ) -> None:
        frm = from_ref.split(",")
        to = to_ref.split(",")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO travel_times "
                "(from_ref, to_ref, from_point, to_point, mode, "
                " distance_m, duration_s) "
                "VALUES ($1, $2, ST_GeogFromText($3), ST_GeogFromText($4), "
                " $5, $6, $7) "
                "ON CONFLICT (from_ref, to_ref, mode) "
                "DO UPDATE SET distance_m=EXCLUDED.distance_m, "
                "duration_s=EXCLUDED.duration_s, cached_at=now()",
                from_ref,
                to_ref,
                f"POINT({frm[0]} {frm[1]})",
                f"POINT({to[0]} {to[1]})",
                mode,
                distance_m,
                duration_s,
            )

    # ── workers ─────────────────────────────────────────────────────────
    async def upsert_workers(self, workers: list[Worker]) -> int:
        async with self.pool.acquire() as conn:
            for w in workers:
                p = w.home_point
                await conn.execute(
                    "INSERT INTO workers "
                    "(id, name, home_address, home_lng, home_lat, home_point, "
                    " transport, available_start, available_end, max_orders, "
                    " phone) "
                    "VALUES ($1,$2,$3,$4,$5, ST_GeogFromText($6), $7,$8,$9,$10,$11) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  name=EXCLUDED.name, home_address=EXCLUDED.home_address, "
                    "  home_lng=EXCLUDED.home_lng, home_lat=EXCLUDED.home_lat, "
                    "  home_point=COALESCE(EXCLUDED.home_point, workers.home_point), "
                    "  transport=EXCLUDED.transport, "
                    "  available_start=EXCLUDED.available_start, "
                    "  available_end=EXCLUDED.available_end, "
                    "  max_orders=EXCLUDED.max_orders, phone=EXCLUDED.phone, "
                    "  updated_at=now()",
                    w.id,
                    w.name,
                    w.home_address,
                    p.lng if p else None,
                    p.lat if p else None,
                    p.wkt() if p else None,
                    w.transport.value,
                    w.available_start,
                    w.available_end,
                    w.max_orders,
                    w.phone,
                )
        return len(workers)

    async def list_workers(self) -> list[Worker]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM workers ORDER BY name")
        out: list[Worker] = []
        for r in rows:
            p = _pt(r["home_lng"], r["home_lat"]) if r["home_lng"] is not None else None
            out.append(
                Worker(
                    id=str(r["id"]),
                    name=r["name"],
                    home_address=r["home_address"],
                    home_point=p,
                    transport=_transport(r["transport"]),
                    available_start=r["available_start"],
                    available_end=r["available_end"],
                    max_orders=r["max_orders"] or 20,
                    phone=r["phone"],
                )
            )
        return out

    # ── orders ──────────────────────────────────────────────────────────
    async def upsert_orders(self, orders: list[Order]) -> int:
        async with self.pool.acquire() as conn:
            for o in orders:
                p = o.site_point
                tw = o.time_window
                await conn.execute(
                    "INSERT INTO orders "
                    "(id, order_no, order_type, merchant, site_address, "
                    " site_lng, site_lat, site_point, date, window_start, "
                    " window_end, service_hours, quantity, amount, note) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7, ST_GeogFromText($8), $9,$10,$11,"
                    " $12,$13,$14,$15) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  order_no=EXCLUDED.order_no, order_type=EXCLUDED.order_type, "
                    "  merchant=EXCLUDED.merchant, site_address=EXCLUDED.site_address, "
                    "  site_lng=EXCLUDED.site_lng, site_lat=EXCLUDED.site_lat, "
                    "  site_point=COALESCE(EXCLUDED.site_point, orders.site_point), "
                    "  date=EXCLUDED.date, window_start=EXCLUDED.window_start, "
                    "  window_end=EXCLUDED.window_end, service_hours=EXCLUDED.service_hours, "
                    "  quantity=EXCLUDED.quantity, amount=EXCLUDED.amount, "
                    "  note=EXCLUDED.note, updated_at=now()",
                    o.id,
                    o.order_no,
                    o.order_type,
                    o.merchant,
                    o.site_address,
                    p.lng if p else None,
                    p.lat if p else None,
                    p.wkt() if p else None,
                    o.date,
                    tw.start if tw else None,
                    tw.end if tw else None,
                    o.service_hours,
                    o.quantity,
                    o.amount,
                    o.note,
                )
        return len(orders)

    async def list_orders(self) -> list[Order]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY order_no")
        out: list[Order] = []
        for r in rows:
            p = _pt(r["site_lng"], r["site_lat"]) if r["site_lng"] is not None else None
            tw = (
                TimeWindow(start=r["window_start"], end=r["window_end"])
                if (r["window_start"] is not None or r["window_end"] is not None)
                else None
            )
            out.append(
                Order(
                    id=str(r["id"]),
                    order_no=r["order_no"],
                    order_type=r["order_type"],
                    merchant=r["merchant"],
                    site_address=r["site_address"],
                    site_point=p,
                    date=r["date"],
                    time_window=tw,
                    service_hours=r["service_hours"] or 0.0,
                    quantity=r["quantity"] or 1,
                    amount=r["amount"],
                    note=r["note"],
                )
            )
        return out

    async def close(self) -> None:
        await self.pool.close()


# ── helpers ───────────────────────────────────────────────────────────────
def _pt(lng, lat) -> Point2D:
    return Point2D(lng=float(lng), lat=float(lat))


def _transport(v) -> TransportMode:
    try:
        return TransportMode.coerce(v)
    except Exception:
        return TransportMode.CAR_SH
