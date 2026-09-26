"""PostgREST + PostGIS data access layer (see compose.yml). Skeleton.

Exposes ``Repository`` as the async data source/sink for routing problems and
their optimized solutions. Backed by PostgREST over a PostGIS database.

NOTE: the ``problem`` / ``solution`` payloads are loose ``dict`` for now. They
will be replaced by the (delayed) ``RoutingProblem`` / ``Solution`` domain models
once the optimizer layer exists. ``httpx`` is intended as the HTTP transport but
is not imported yet, so stubs stay lint-clean.
"""

from __future__ import annotations

from typing import Any


class Repository:
    """Async data source/sink backed by PostgREST over PostGIS.

    Skeleton: configuration is stored, but no I/O happens yet.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key

    async def load_problem(self, problem_id: str) -> dict[str, Any]:
        """Fetch one routing problem (depot + stops) from PostgREST.

        Placeholder — becomes ``RoutingProblem`` once models exist.
        """
        raise NotImplementedError

    async def save_solution(
        self, problem_id: str, solution: dict[str, Any]
    ) -> None:
        """Persist an optimized solution back to PostgREST.

        Placeholder — ``solution`` becomes ``Solution`` once models exist.
        """
        raise NotImplementedError
