"""storage — the single asyncpg/PostGIS persistence layer (:class:`DB`).

No job queue, event log, or ORM: a dispatch runs synchronously, and the
database stores workers/orders plus the cached Amap travel-time results.
"""

from .db import DB

__all__ = ["DB"]
