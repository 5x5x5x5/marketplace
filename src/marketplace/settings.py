"""App settings from environment (with optional .env).

`DATABASE_URL` selects the backend: Postgres in production/CI, SQLite locally and
in the test suite (no Docker needed). SQLAlchemy keeps the schema portable.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Default to a local SQLite file so `uv run` and the tests work with no setup.
    # Point DATABASE_URL at Postgres (see docker-compose.yml) for real deployments.
    database_url: str = "sqlite+pysqlite:///./marketplace.db"

    # Shared HMAC secret for pilot auth. MUST be overridden outside local dev.
    marketplace_secret: str = "dev-insecure-secret"

    quote_ttl_minutes: int = 5
    offer_ttl_minutes: int = 2
    token_ttl_hours: int = 24


settings = Settings()
