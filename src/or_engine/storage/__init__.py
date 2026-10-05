"""storage — the persistence layer.

Provides two interchangeable backends for the travel-time cache:

* :class:`DB` — asyncpg/PostGIS (used by the REST/MCP server)
* :class:`LocalCache` — SQLite file (used by the CLI's ``--cache`` flag)
"""

from .cache import LocalCache
from .db import DB

__all__ = ["DB", "LocalCache"]
