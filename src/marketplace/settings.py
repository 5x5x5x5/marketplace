"""App settings from environment (with optional .env).

`DATABASE_URL` selects the backend: Postgres in production/CI, SQLite locally and
in the test suite (no Docker needed). SQLAlchemy keeps the schema portable.
"""

from decimal import Decimal
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Default to a local SQLite file so `uv run` and the tests work with no setup.
    # Point DATABASE_URL at Postgres (see docker-compose.yml) for real deployments.
    database_url: str = "sqlite+pysqlite:///./marketplace.db"

    # Connection pool (ignored for SQLite). Sized against Starlette's default
    # 40-token threadpool: 10 kept open + 30 overflow = 40, so every thread
    # that can run a handler at once can hold a connection. A checkout that
    # waits past the timeout becomes a 503 with Retry-After instead of a 30 s
    # stall and a 500: under overload, queued requests hold threads while
    # requests that already hold connections wait for a thread to commit, and
    # only the timeout breaks that, so a short one sheds load early.
    # Per instance: replicas x (size + overflow) must fit Postgres's
    # max_connections with headroom. DB_POOL=null hands pooling to an external
    # pooler (fresh connection per checkout; size/overflow/timeout unused).
    db_pool: Literal["queue", "null"] = "queue"
    db_pool_size: int = 10
    db_max_overflow: int = 30
    db_pool_timeout_seconds: float = 5.0

    # Sized for human sellers answering a browse booking, not instant dispatch.
    quote_ttl_minutes: int = 15
    offer_ttl_minutes: int = 120

    # Payments. STRIPE_SECRET_KEY set → real Stripe adapter; unset → deterministic
    # in-memory fake (dev/tests, no account needed).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Browser-side key for Stripe.js (the Payment Element on the booking
    # page). Required with STRIPE_SECRET_KEY; must be a pk_ key, since it is
    # published in page source.
    stripe_publishable_key: str = ""
    # Dev/demo only: the fake provider leaves every charge PENDING so the
    # browser flow shows the payment step and the dev webhook settles it.
    # Refused alongside a Stripe key.
    fake_charges_pending: bool = False
    # The fake provider's webhooks are unsigned JSON with guessable ids, so
    # POST /v1/payments/webhook 404s in fake mode unless this is on. Dev,
    # tests and demos only: refused at startup alongside a Stripe key.
    allow_unsigned_webhooks: bool = False
    currency: str = "usd"  # ponytail: single currency; multi-currency is a fork concern
    payment_ttl_minutes: int = 30  # AWAITING_PAYMENT older than this expires on sweep
    onboarding_return_url: str = "http://localhost:8000/onboarded"
    # Webhook outbox: a handler failure is persisted FAILED and retried by the
    # maintenance loop after webhook_retry_base_seconds * 5**(attempts-1); at
    # webhook_max_attempts the event is dead-lettered and admins notified.
    webhook_retry_base_seconds: int = 60
    webhook_max_attempts: int = 5

    # Auth. Sessions are DB-backed and revocable; the admin account is seeded
    # from these two settings at startup (empty -> no admin, logged).
    session_ttl_hours: int = 72
    admin_email: str = ""
    admin_password: str = ""
    base_url: str = "http://localhost:8000"  # used in verification/reset links
    cookie_secure: bool = False  # set True behind TLS in production
    # When on, creating a job, accepting an offer and creating a listing need
    # a verified email (enforced in the services, so API and pages agree).
    require_verified_email: bool = False

    # Notifications: transactional outbox drained by the in-process loop.
    notify_drain_seconds: int = 5
    notify_max_attempts: int = 5
    sweep_interval_seconds: int = 60
    # Gates the in-process maintenance loop (outbox drain, sweeps,
    # housekeeping). With several replicas, leave it on for exactly one; every
    # job in it is safe to run concurrently, but N runners is N times the work.
    maintenance_enabled: bool = True

    # Mail delivery: SMTP_HOST set -> stdlib SMTP adapter (any provider's SMTP
    # endpoint, or Mailpit locally); empty -> console adapter (logs only).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = "marketplace@localhost"

    # Observability. JSON logs by default; LOG_FORMAT=plain for dev readability.
    log_format: str = "json"

    # Retention (days): swept tables stay bounded. PENDING outbox rows are
    # never reaped regardless of age.
    retention_idempotency_days: int = 7
    retention_webhooks_days: int = 30
    retention_notifications_days: int = 30

    # Disputes: buyer-opened arbitration on completed jobs.
    dispute_window_days: int = 7
    chargeback_fee_usd: Decimal = Decimal("15.00")  # provider fee on a lost chargeback

    # API hardening. trusted_hosts/* and empty cors_origins = open (dev);
    # narrow both in production. Bodies over max_body_bytes get a 413.
    trusted_hosts: list[str] = ["*"]
    cors_origins: list[str] = []
    max_body_bytes: int = 1_048_576

    @model_validator(mode="after")
    def _webhooks_are_verifiable(self) -> "Settings":
        if self.stripe_secret_key and not self.stripe_webhook_secret:
            # Every real webhook would fail its signature check: charges
            # would be taken and never settle.
            raise ValueError("STRIPE_SECRET_KEY is set but STRIPE_WEBHOOK_SECRET is not")
        if self.stripe_secret_key and self.allow_unsigned_webhooks:
            raise ValueError("ALLOW_UNSIGNED_WEBHOOKS must be off when STRIPE_SECRET_KEY is set")
        if self.stripe_secret_key and not self.stripe_publishable_key:
            # Charges wait for confirmation in the buyer's browser; without
            # the browser key no buyer could ever pay.
            raise ValueError("STRIPE_SECRET_KEY is set but STRIPE_PUBLISHABLE_KEY is not")
        if self.stripe_publishable_key and not self.stripe_publishable_key.startswith("pk_"):
            # It is rendered into page source: a secret (sk_) or restricted
            # (rk_) key pasted here would be published to every buyer.
            raise ValueError("STRIPE_PUBLISHABLE_KEY must be a publishable key (pk_...)")
        if self.stripe_secret_key and self.fake_charges_pending:
            raise ValueError("FAKE_CHARGES_PENDING is a fake-provider demo switch; unset it")
        return self


settings = Settings()
