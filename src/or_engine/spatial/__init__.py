"""spatial — geocoding (Amap) and travel-time matrix computation.

The domain/solver layers are handed a pre-built :class:`TravelMatrix`;
they never touch the Amap HTTP API or the PostGIS cache directly.
"""

from .amap import AmapClient, DirectionResponse, Geocode, TransportMode
from .distance import build_euclidean_matrix, euclidean_m, haversine
from .travel import TravelMatrix, build_travel_matrix
from .api_error import AmapAPIError, record_geocode_failure, record_direction_failure
from .failures import FailureTracker, FailureRecord

__all__ = [
    "AmapClient",
    "TransportMode",
    "Geocode",
    "DirectionResponse",
    "build_euclidean_matrix",
    "euclidean_m",
    "haversine",
    "TravelMatrix",
    "build_travel_matrix",
]
