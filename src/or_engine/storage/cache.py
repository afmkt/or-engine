"""Local SQLite cache for AMap geocode and travel-time results.

Used by the CLI when the user passes ``--cache <path>``.  Provides a file-
backed cache that mirrors the interface of :class:`~or_engine.storage.db.DB`
but requires no server -- just the standard-library ``sqlite3`` module.

Two cache tables live side by side:

* ``geocode_cache``   -- address -> latitude/longitude (city-biased)
* ``travel_cache``    -- (from_ref, to_ref, mode) -> distance/duration

Both are populated on cache-miss and checked before every API call, so
repeated runs with the same inputs reuse results without hitting AMap.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from ..models import Point2D
from ..spatial.amap import Geocode


class LocalCache:
    """SQLite-backed cache for geocode and travel-matrix results.

    Thread-safe: a single :class:`sqlite3.Connection` is shared across
    async tasks via :func:`asyncio.to_thread` and guarded by a
    :class:`threading.Lock`.
    """

    _SCHEMA = """\
        CREATE TABLE IF NOT EXISTS geocode_cache (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            address    TEXT NOT NULL,
            city       TEXT NOT NULL DEFAULT '',
            name       TEXT,
            lat        REAL NOT NULL,
            lng        REAL NOT NULL,
            cached_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(address, city)
        );
        CREATE INDEX IF NOT EXISTS idx_geocode_lookup
            ON geocode_cache (address, city);

        CREATE TABLE IF NOT EXISTS travel_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            from_ref     TEXT NOT NULL,
            to_ref       TEXT NOT NULL,
            mode         TEXT NOT NULL DEFAULT 'driving',
            distance_m   REAL,
            duration_s   REAL,
            cached_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(from_ref, to_ref, mode)
        );
        CREATE INDEX IF NOT EXISTS idx_travel_lookup
            ON travel_cache (from_ref, to_ref, mode);
    """

    def __init__(self, path: str | None = None) -> None:
        self._path: str = path or f"/tmp/or-engine-cache-{int(time.time())}.db"
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection
        try:
            self._conn = sqlite3.connect(
                self._path, check_same_thread=False, timeout=10,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()
        except Exception:
            # Fall back to in-memory cache if the file can't be opened
            self._path = ":memory:"
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()

    # -- geocode -------------------------------------------------------

    def _get_geocode_sync(
        self, address: str, city: str = "",
    ) -> tuple[float, float] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT lat, lng, name FROM geocode_cache "
                "WHERE address=? AND city=?",
                (address, city),
            )
            row = cur.fetchone()
            if row:
                return Geocode(row[2] or "", Point2D(lat=row[0], lng=row[1]), None)
            return None

    async def get_geocode(
        self, address: str, city: str = "",
    ) -> tuple[float, float] | None:
        return await asyncio.to_thread(
            self._get_geocode_sync, address, city,
        )

    def _save_geocode_sync(
        self, address: str, city: str,
        lat: float, lng: float, name: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode_cache "
                "(address, city, name, lat, lng) VALUES (?,?,?,?,?)",
                (address, city, name, lat, lng),
            )
            self._conn.commit()

    async def save_geocode(
        self, address: str, city: str = "",
        name: str = "", lat: float = 0.0, lng: float = 0.0,
    ) -> None:
        await asyncio.to_thread(
            self._save_geocode_sync, address, city, lat, lng, name,
        )

    # -- travel matrix -------------------------------------------------

    def _get_travel_pair_sync(
        self, from_ref: str, to_ref: str, mode: str = "driving",
    ) -> tuple | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT distance_m, duration_s FROM travel_cache "
                "WHERE from_ref=? AND to_ref=? AND mode=?",
                (from_ref, to_ref, mode),
            )
            row = cur.fetchone()
            if row:
                return {"distance_m": row[0], "duration_s": row[1]}
            return None

    async def get_travel_pair(
        self, from_ref: str, to_ref: str, mode: str = "driving",
    ) -> tuple[float | None, float | None] | None:
        return await asyncio.to_thread(
            self._get_travel_pair_sync, from_ref, to_ref, mode,
        )

    def _save_travel_pair_sync(
        self, from_ref: str, to_ref: str,
        mode: str,
        distance_m: float | None, duration_s: float | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO travel_cache "
                "(from_ref, to_ref, mode, distance_m, duration_s) "
                "VALUES (?,?,?,?,?)",
                (from_ref, to_ref, mode, distance_m, duration_s),
            )
            self._conn.commit()

    async def save_travel_pair(
        self,
        from_ref: str,
        to_ref: str,
        mode: str = "driving",
        distance_m: float | None = None,
        duration_s: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._save_travel_pair_sync,
            from_ref, to_ref, mode, distance_m, duration_s,
        )

    # -- diagnostics ---------------------------------------------------

    def _stats_sync(self) -> dict[str, int]:
        with self._lock:
            geocode_n = self._conn.execute(
                "SELECT COUNT(*) FROM geocode_cache",
            ).fetchone()[0]
            travel_n = self._conn.execute(
                "SELECT COUNT(*) FROM travel_cache",
            ).fetchone()[0]
        return {"geocode": geocode_n, "travel": travel_n}

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
