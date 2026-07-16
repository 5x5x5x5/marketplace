"""SQLAlchemy ORM entities — the persisted data model.

Portable across Postgres (production) and SQLite (local/tests): enums are stored
as their string values via `native_enum=False`, timestamps and ids default
Python-side, money is `Numeric(12, 2)` (→ `Decimal`). The API never returns these
objects directly — endpoints map them to the Pydantic views in `models.py`.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import (
    AdjustmentKind,
    DisputeSource,
    DisputeStatus,
    EmailTokenPurpose,
    EventKind,
    JobStatus,
    NotificationStatus,
    OfferStatus,
    PaymentStatus,
    PayoutStatus,
    ReportStatus,
    ReportTargetKind,
    UserRole,
    UserStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    Postgres round-trips tz-aware values; SQLite drops the tzinfo. This coerces
    both directions to UTC-aware so comparisons never mix naive and aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [str(m.value) for m in enum_cls]


def _enum(enum_type: type[StrEnum]) -> SAEnum:
    # Store the enum's string values (not names), non-native for SQLite portability.
    return SAEnum(enum_type, native_enum=False, values_callable=_enum_values, length=32)


_MONEY = Numeric(12, 2)
_TS = UTCDateTime()


class Base(DeclarativeBase):
    pass


class ServiceType(Base):
    __tablename__ = "service_types"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    base_buyer_price: Mapped[Decimal] = mapped_column(_MONEY)
    base_seller_payout: Mapped[Decimal] = mapped_column(_MONEY)


class Pipeline(Base):
    __tablename__ = "pipelines"

    service_type_id: Mapped[str] = mapped_column(ForeignKey("service_types.id"), primary_key=True)
    buyer: Mapped[list[str]] = mapped_column(JSON, default=list)
    seller: Mapped[list[str]] = mapped_column(JSON, default=list)


class PlatformConfig(Base):
    """Singleton row (id=1): margin floor + active strategy + adjuster params."""

    __tablename__ = "platform_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    margin_absolute: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal(0))
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal(0))
    ceiling_multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(3))
    fee_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.029"))
    fee_fixed: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0.30"))
    matching_strategy: Mapped[str] = mapped_column(String(64), default="cheapest_payout")
    adjuster_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SellerProfile(Base):
    __tablename__ = "seller_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tier: Mapped[str] = mapped_column(String(64), default="standard")
    capacity: Mapped[int] = mapped_column(default=1)
    rating_count: Mapped[int] = mapped_column(default=0)
    rating_sum: Mapped[int] = mapped_column(default=0)
    completed_jobs: Mapped[int] = mapped_column(default=0)
    provider_account_id: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    payments_ready: Mapped[bool] = mapped_column(default=False)  # set by account webhook

    @property
    def rating(self) -> float | None:
        return (self.rating_sum / self.rating_count) if self.rating_count else None


class BuyerProfile(Base):
    __tablename__ = "buyer_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    completed_jobs: Mapped[int] = mapped_column(default=0)
    rating_count: Mapped[int] = mapped_column(default=0)
    rating_sum: Mapped[int] = mapped_column(default=0)

    @property
    def rating(self) -> float | None:
        return (self.rating_sum / self.rating_count) if self.rating_count else None


class Availability(Base):
    __tablename__ = "availability"
    __table_args__ = (UniqueConstraint("seller_id", "service_type_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[str] = mapped_column(String(128), index=True)
    service_type_id: Mapped[str] = mapped_column(ForeignKey("service_types.id"), index=True)
    since: Mapped[datetime] = mapped_column(_TS, default=_now)


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    service_type_id: Mapped[str] = mapped_column(String(128))
    buyer_price: Mapped[Decimal] = mapped_column(_MONEY)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    expires_at: Mapped[datetime] = mapped_column(_TS)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    service_type_id: Mapped[str] = mapped_column(String(128), index=True)
    buyer_price: Mapped[Decimal] = mapped_column(_MONEY)
    seller_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    seller_payout: Mapped[Decimal | None] = mapped_column(_MONEY, default=None)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus), default=JobStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    accepted_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    service_type_id: Mapped[str] = mapped_column(String(128))
    seller_id: Mapped[str] = mapped_column(String(128), index=True)
    seller_payout: Mapped[Decimal] = mapped_column(_MONEY)
    status: Mapped[OfferStatus] = mapped_column(
        _enum(OfferStatus), default=OfferStatus.OFFERED, index=True
    )
    offered_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    expires_at: Mapped[datetime] = mapped_column(_TS)
    responded_at: Mapped[datetime | None] = mapped_column(_TS, default=None)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    buyer_price: Mapped[Decimal] = mapped_column(_MONEY)
    seller_payout: Mapped[Decimal] = mapped_column(_MONEY)
    margin: Mapped[Decimal] = mapped_column(_MONEY)
    completed_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128))
    seller_id: Mapped[str] = mapped_column(String(128), index=True)
    rating: Mapped[int] = mapped_column()
    comment: Mapped[str | None] = mapped_column(String(2000), default=None)
    comment_hidden: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)

    @property
    def public_comment(self) -> str | None:
        """Single home of the takedown invariant: non-admin views read this."""
        return None if self.comment_hidden else self.comment


class SellerReview(Base):
    """Seller→buyer review. Mirror of `Review`; the buyer aggregate it feeds
    is display-only — it gates nothing (see the 2026-07-14 design)."""

    __tablename__ = "seller_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    seller_id: Mapped[str] = mapped_column(String(128))
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    rating: Mapped[int] = mapped_column()
    comment: Mapped[str | None] = mapped_column(String(2000), default=None)
    comment_hidden: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)

    @property
    def public_comment(self) -> str | None:
        """Single home of the takedown invariant: non-admin views read this."""
        return None if self.comment_hidden else self.comment


class Report(Base):
    """User-filed abuse report. Paper trail only: resolving one never
    auto-suspends or auto-hides — admins act with the explicit tools."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("reporter_id", "target_kind", "target_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reporter_id: Mapped[str] = mapped_column(String(128), index=True)
    target_kind: Mapped[ReportTargetKind] = mapped_column(_enum(ReportTargetKind))
    target_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(2000))
    status: Mapped[ReportStatus] = mapped_column(
        _enum(ReportStatus), default=ReportStatus.OPEN, index=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(_TS, default=None)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now, index=True)


class Payment(Base):
    """Buyer charge for a job (1:1). Cash record — `Transaction` stays the margin ledger."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[Decimal] = mapped_column(_MONEY)
    fee_estimate: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus), default=PaymentStatus.PENDING, index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str] = mapped_column(String(256), index=True)
    client_secret: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class Payout(Base):
    """Seller transfer for a completed job (1:1)."""

    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    seller_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[Decimal] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[PayoutStatus] = mapped_column(
        _enum(PayoutStatus), default=PayoutStatus.PENDING, index=True
    )
    provider_transfer_id: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class WebhookEvent(Base):
    """Processed provider events — the dedup ledger (replayed events no-op)."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_event_id: Mapped[str] = mapped_column(String(256), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class IdempotencyRecord(Base):
    """Stored response for a client Idempotency-Key (scoped per principal)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("principal", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal: Mapped[str] = mapped_column(String(150))  # "role:sub"
    key: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(256))
    response_status: Mapped[int] = mapped_column()
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class User(Base):
    """Identity + credential. One account carries exactly ONE role; the same
    email may register once per role. Domain records (Buyer/SellerProfile)
    stay separate and are keyed by this id.

    String pk (uuid4 hex for real signups) so the test fixture can use the
    test's plain sub string as the id — existing identity assertions hold."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", "role"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=lambda: uuid.uuid4().hex)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole))
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(128))
    email_verified: Mapped[bool] = mapped_column(default=False)
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus), default=UserStatus.ACTIVE, index=True
    )
    suspended_reason: Mapped[str | None] = mapped_column(String(2000), default=None)
    suspended_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class AuthSession(Base):
    """A revocable login. Stores only the sha256 of the opaque bearer —
    a DB leak never yields usable tokens. Logout/ban/reset delete rows."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    expires_at: Mapped[datetime] = mapped_column(_TS)


class EmailToken(Base):
    """Single-use verification/reset token (sha256-stored, like sessions)."""

    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[EmailTokenPurpose] = mapped_column(_enum(EmailTokenPurpose))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(_TS)
    used_at: Mapped[datetime | None] = mapped_column(_TS, default=None)


class Notification(Base):
    """Transactional-outbox row: enqueued inside the domain transaction,
    delivered by the drainer. `email` and `payload` are snapshots taken at
    enqueue time — the drainer never re-queries mutable domain state."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    kind: Mapped[EventKind] = mapped_column(_enum(EventKind))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus), default=NotificationStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(_TS, default=_now, index=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
    last_error: Mapped[str | None] = mapped_column(String(512), default=None)


class NotificationMute(Base):
    """Sparse per-user opt-out: a row means muted, absence means subscribed.
    Money kinds (MUST_SEND in notifications.py) never consult this table."""

    __tablename__ = "notification_mutes"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[EventKind] = mapped_column(_enum(EventKind))
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class Dispute(Base):
    """One per job. Buyer-opened arbitration or a provider chargeback; the
    money outcome lives in `adjustments`, never by editing booked rows."""

    __tablename__ = "disputes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    source: Mapped[DisputeSource] = mapped_column(_enum(DisputeSource))
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str] = mapped_column(String(2000))
    status: Mapped[DisputeStatus] = mapped_column(
        _enum(DisputeStatus), default=DisputeStatus.OPEN, index=True
    )
    refund_amount: Mapped[Decimal | None] = mapped_column(_MONEY, default=None)
    clawback_amount: Mapped[Decimal | None] = mapped_column(_MONEY, default=None)
    resolution_note: Mapped[str | None] = mapped_column(String(2000), default=None)
    provider_dispute_id: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(_TS, default=None)


class Adjustment(Base):
    """Append-only money corrections. Amounts are positive; kind carries the
    sign. Transaction rows are immutable — this ledger is the only place a
    resolution touches the books."""

    __tablename__ = "adjustments"
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_adjustments_amount_nonneg"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    dispute_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("disputes.id"), index=True)
    kind: Mapped[AdjustmentKind] = mapped_column(_enum(AdjustmentKind))
    amount: Mapped[Decimal] = mapped_column(_MONEY)
    provider_ref: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
