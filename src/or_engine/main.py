"""or_engine server entrypoint (``python -m or_engine`` / ``or-engine``).

Runs the FastAPI app via uvicorn; host/port come from :data:`or_engine.config.settings`
(i.e. the ``HOST`` / ``PORT`` env vars or ``.env``).
"""

from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

from .config import settings

load_dotenv()


def main() -> None:
    uvicorn.run(
        "or_engine.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
