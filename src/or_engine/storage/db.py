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

-- file_uploads: raw bytes + content hash of every uploaded workbook, so a
-- parsed row and a dispatch result can be tied back to the exact source.
CREATE TABLE IF NOT EXISTS file_uploads (
id            UUID PRIMARY KEY,
kind          TEXT NOT NULL,           -- 'workers' | 'orders' | 'working_hours'
filename      TEXT,
content_type  TEXT,
sha256        TEXT NOT NULL,           -- content hash (dedup key / audit record)
size_bytes    BIGINT NOT NULL,
payload       BYTEA NOT NULL,          -- raw .xlsx bytes (Postgres TOAST, ~1 GB cap)
uploaded_by   TEXT,
uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_file_uploads_kind ON file_uploads (kind);
CREATE INDEX IF NOT EXISTS idx_file_uploads_sha256 ON file_uploads (sha256);

-- working_hours (工作工时表): product description -> on-site labour hours.
-- Foreign key back to the upload that supplied these rows.
CREATE TABLE IF NOT EXISTS working_hours (
product          TEXT PRIMARY KEY,
hours            DOUBLE PRECISION NOT NULL DEFAULT 0,
note             TEXT,
source_upload_id UUID REFERENCES file_uploads(id) ON DELETE SET NULL,
updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- traceability: tie every parsed worker/order row to its source upload.
ALTER TABLE workers ADD COLUMN IF NOT EXISTS source_upload_id UUID;
ALTER TABLE orders  ADD COLUMN IF NOT EXISTS source_upload_id UUID;
DO $or_engine$
BEGIN
     IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'workers_source_upload_fk') THEN
          ALTER TABLE workers ADD CONSTRAINT workers_source_upload_fk
               FOREIGN KEY (source_upload_id) REFERENCES file_uploads(id) ON DELETE SET NULL;
     END IF;
     IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'orders_source_upload_fk') THEN
          ALTER TABLE orders ADD CONSTRAINT orders_source_upload_fk
               FOREIGN KEY (source_upload_id) REFERENCES file_uploads(id) ON DELETE SET NULL;
     END IF;
END $or_engine$;
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
    async def upsert_workers(
        self, workers: list[Worker],
        source_upload_id: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            for w in workers:
                p = w.home_point
                await conn.execute(
                    "INSERT INTO workers "
                    "(id, name, home_address, home_lng, home_lat, home_point, "
                    " transport, available_start, available_end, max_orders, "
                    " phone, source_upload_id) "
                    "VALUES ($1,$2,$3,$4,$5, ST_GeogFromText($6), $7,$8,$9,$10,$11,$12) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  name=EXCLUDED.name, home_address=EXCLUDED.home_address, "
                    "  home_lng=EXCLUDED.home_lng, home_lat=EXCLUDED.home_lat, "
                    "  home_point=COALESCE(EXCLUDED.home_point, workers.home_point), "
                    "  transport=EXCLUDED.transport, "
                    "  available_start=EXCLUDED.available_start, "
                    "  available_end=EXCLUDED.available_end, "
                    "  max_orders=EXCLUDED.max_orders, phone=EXCLUDED.phone, source_upload_id=COALESCE(EXCLUDED.source_upload_id, workers.source_upload_id), "
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
                    source_upload_id,
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
    async def upsert_orders(
        self, orders: list[Order],
        source_upload_id: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            for o in orders:
                p = o.site_point
                tw = o.time_window
                await conn.execute(
                    "INSERT INTO orders "
                    "(id, order_no, order_type, merchant, site_address, "
                    " site_lng, site_lat, site_point, date, window_start, "
                    " window_end, service_hours, quantity, amount, note, source_upload_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7, ST_GeogFromText($8), $9,$10,$11,"
                    " $12,$13,$14,$15,$16) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  order_no=EXCLUDED.order_no, order_type=EXCLUDED.order_type, "
                    "  merchant=EXCLUDED.merchant, site_address=EXCLUDED.site_address, "
                    "  site_lng=EXCLUDED.site_lng, site_lat=EXCLUDED.site_lat, "
                    "  site_point=COALESCE(EXCLUDED.site_point, orders.site_point), "
                    "  date=EXCLUDED.date, window_start=EXCLUDED.window_start, "
                    "  window_end=EXCLUDED.window_end, service_hours=EXCLUDED.service_hours, "
                    "  quantity=EXCLUDED.quantity, amount=EXCLUDED.amount, "
                    "  note=EXCLUDED.note, source_upload_id=COALESCE(EXCLUDED.source_upload_id, orders.source_upload_id), updated_at=now()",
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
                    source_upload_id,
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

    # ── file_uploads: raw workbook bytes + content hash ───────────────────
    # The raw .xlsx bytes are stored as BYTEA (Postgres TOAST, ~1 GB cap).
    # Keep them in-DB while files stay small; at scale offload the bytes to
    # object storage and retain only the pointer + sha256 here.
    async def save_upload(
        self,
        *,
        kind: str,
        payload: bytes,
        filename: str | None = None,
        content_type: str | None = None,
        uploaded_by: str | None = None,
    ) -> dict:
        """Persist raw bytes in ``file_uploads`` and return its record.

        ``sha256`` is the content hash of *payload* -- the dedup key and the
        traceability link back to the exact source bytes. Returns
        ``deduplicated: True`` when an identical prior upload is reused.
        """
        import hashlib
        import uuid

        sha = hashlib.sha256(payload).hexdigest()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, filename, content_type, sha256, size_bytes, "
                "uploaded_by, uploaded_at FROM file_uploads "
                "WHERE kind=$1 AND sha256=$2 ORDER BY uploaded_at LIMIT 1",
                kind,
                sha,
            )
            if row is not None:
                return {k: row[k] for k in row.keys()} | {"deduplicated": True}
            uid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO file_uploads "
                "(id, kind, filename, content_type, sha256, size_bytes, payload, "
                " uploaded_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                uid,
                kind,
                filename,
                content_type,
                sha,
                len(payload),
                payload,
                uploaded_by,
            )
        return {
            "id": uid,
            "kind": kind,
            "filename": filename,
            "content_type": content_type,
            "sha256": sha,
            "size_bytes": len(payload),
            "deduplicated": False,
        }

    async def get_upload_by_sha256(self, sha256: str) -> dict | None:
        """Return a ``file_uploads`` record by content hash, or ``None``."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, filename, content_type, sha256, size_bytes, "
                "uploaded_by, uploaded_at FROM file_uploads WHERE sha256=$1 "
                "ORDER BY uploaded_at LIMIT 1",
                sha256,
            )
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    async def list_uploads(self, kind: str | None = None) -> list[dict]:
        """Recent uploads (metadata only; the ``payload`` bytes are excluded)."""
        cols = (
            "id, kind, filename, content_type, sha256, size_bytes, "
            "uploaded_by, uploaded_at"
        )
        async with self.pool.acquire() as conn:
            if kind:
                rows = await conn.fetch(
                    f"SELECT {cols} FROM file_uploads WHERE kind=$1 "
                    "ORDER BY uploaded_at DESC LIMIT 200",
                    kind,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {cols} FROM file_uploads "
                    "ORDER BY uploaded_at DESC LIMIT 200"
                )
        return [dict(r) for r in rows]

    # ── working_hours ───────────────────────────────────────────────────
    async def upsert_working_hours(
        self,
        hours: list[WorkingHour],
        source_upload_id: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            for h in hours:
                await conn.execute(
                    "INSERT INTO working_hours "
                    "(product, hours, note, source_upload_id) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (product) DO UPDATE SET "
                    "  hours=EXCLUDED.hours, "
                    "  note=COALESCE(EXCLUDED.note, working_hours.note), "
                    "  source_upload_id=COALESCE(EXCLUDED.source_upload_id, "
                    "    working_hours.source_upload_id), updated_at=now()",
                    h.product,
                    h.hours,
                    h.note,
                    source_upload_id,
                )
        return len(hours)

    async def list_working_hours(self) -> list[WorkingHour]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM working_hours ORDER BY product")
        out: list[WorkingHour] = []
        for r in rows:
            out.append(
                WorkingHour(
                    product=r["product"],
                    hours=r["hours"] or 0.0,
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
