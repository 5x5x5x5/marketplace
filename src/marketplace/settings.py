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

    quote_ttl_minutes: int = 5
    offer_ttl_minutes: int = 2

    # Payments. STRIPE_SECRET_KEY set → real Stripe adapter; unset → deterministic
    # in-memory fake (dev/tests, no account needed).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "usd"  # ponytail: single currency; multi-currency is a fork concern
    payment_ttl_minutes: int = 30  # AWAITING_PAYMENT older than this expires on sweep
    onboarding_return_url: str = "http://localhost:8000/onboarded"

    # Auth. Sessions are DB-backed and revocable; the admin account is seeded
    # from these two settings at startup (empty -> no admin, logged).
    session_ttl_hours: int = 72
    admin_email: str = ""
    admin_password: str = ""
    base_url: str = "http://localhost:8000"  # used in verification/reset links

    # Notifications: transactional outbox drained by the in-process loop.
    notify_drain_seconds: int = 5
    notify_max_attempts: int = 5
    sweep_interval_seconds: int = 60


settings = Settings()
