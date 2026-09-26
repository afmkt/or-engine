# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Dependency specs first, so the large install layer is cached even when the
# Python source changes.
COPY uv.lock ./
COPY pyproject.toml ./
COPY README.md ./

# uv sync does an editable install of this project (uv_build + src/ layout), so
# the package source must exist at sync time.
COPY src/ ./src/

# Install production deps + the project itself (frozen from lock, no dev extras)
# into the default /app/.venv. The shared-wheel cache keeps rebuilds fast.
RUN --mount=type=cache,target=/root/.cache/uv \
     uv sync --frozen --no-dev

EXPOSE 8000

# `uv run or-engine` activates the .venv that `uv sync` created and runs the
# project's declared console script (pyproject.toml: `or-engine =
# "or_engine.server:main"`), which in turn runs `create_app()` under uvicorn.
CMD ["uv", "run", "or-engine", "--host", "0.0.0.0", "--port", "8000"]
