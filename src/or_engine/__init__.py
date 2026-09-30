"""or-engine — worker-to-order dispatch.

A single-day VRPTW over installation workers and work orders:

import workers & orders from Excel
-> geocode addresses via Amap
-> build a travel-time matrix (Amap, cached in PostGIS)
-> route every worker through its assigned order sites (OR-Tools VRPTW)
-> write the result back to Excel

Everything is exposed through a REST (FastAPI / OpenAPI) and an MCP server.
Layers: :mod:`models`, :mod:`spatial`, :mod:`storage`, :mod:`solver`,
:mod:`engine`, :mod:`excel`, :mod:`api`.
"""

from .models import (
    AssignRoute,
    DispatchResult,
    DispatchStatus,
    Order,
    OrderStop,
    Point2D,
    TimeWindow,
    TransportMode,
    Worker,
)
from .spatial import AmapClient, TravelMatrix

__all__ = [
    "AssignRoute",
    "DispatchResult",
    "DispatchStatus",
    "Order",
    "OrderStop",
    "Point2D",
    "TimeWindow",
    "TransportMode",
    "Worker",
    "AmapClient",
    "TravelMatrix",
]

__version__ = "0.1.0"
