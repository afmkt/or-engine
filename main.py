"""Root entry: run the or-engine FastAPI app.

uvicorn or_engine.api.app:create_app --factory
# or
python main.py
# or, via the console script installed by uv (or-engine = or_engine.main:main):
or-engine
"""

import uvicorn
from dotenv import load_dotenv

from or_engine.config import settings

load_dotenv()


def main() -> None:
    """Start the OpenAPI (FastAPI) server via uvicorn."""
    uvicorn.run(
        "or_engine.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
