"""SQLAlchemy ORM entities — the persisted data model.

Portable across Postgres (production) and SQLite (local/tests): enums are stored
as their string values via `native_enum=False`, timestamps and ids default
Python-side, money is `Numeric(12, 2)` (→ `Decimal`). The API never returns these
objects directly — endpoints map them to the Pydantic views in `models.py`.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import JobStatus, OfferStatus


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


def _enum(enum_type: type[JobStatus] | type[OfferStatus]) -> SAEnum:
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

    @property
    def rating(self) -> float | None:
        return (self.rating_sum / self.rating_count) if self.rating_count else None


class BuyerProfile(Base):
    __tablename__ = "buyer_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    completed_jobs: Mapped[int] = mapped_column(default=0)


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
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now, index=True)
