"""Typed application settings.

Only the settings the current stage actually needs. Everything else
(the async job queue, event log, PostgREST/Janux host names) was part
of the earlier over-complex design and has been removed.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Amap (高德) ────────────────────────────────────────────────────
    amap_api_key: str = ""
    amap_city: str = "上海"    # city hint for geocoding (CLI --city overrides)

    # ── persistence ────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:postgres@localhost:5432/geospatial_db"

    # ── solver ─────────────────────────────────────────────────────────
    solver_timeout_s: float = 30.0
    # Per-transport-mode multiplier applied to Amap *driving* travel time.
    # Driving is the base; slower/more-restricted modes take longer.
    transport_speed_factors: dict[str, float] = {
        "ebike": 1.6,
        "car_sh": 1.0,
        "car_out": 1.15,  # out-of-town plate, city driving restrictions
    }

    # ── server ─────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
