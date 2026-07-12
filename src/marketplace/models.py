"""API-facing Pydantic models and shared domain types.

These are the request/response DTOs and the domain enums/money helper. The
persisted entities live in `entities.py` (SQLAlchemy); this module never imports
the DB layer. Response views deliberately omit the other side's number — buyer
views carry no `seller_payout`, seller views carry no `buyer_price`.

Money is `Decimal`, quantized to 2 dp, and serialized as JSON strings.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CENTS = Decimal("0.01")


def to_money(value: float | int | str | Decimal) -> Decimal:
    """Quantize any numeric to a 2-dp Decimal (half-up). The single money gate."""
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


class Side(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class JobStatus(StrEnum):
    PENDING = "pending"  # created, an offer is out (or being (re)matched)
    AWAITING_PAYMENT = "awaiting_payment"  # seller committed; buyer's charge not yet secured
    ACCEPTED = "accepted"  # a seller committed AND the money is secured
    COMPLETED = "completed"
    EXPIRED = "expired"  # no seller took it (or payment never arrived)
    CANCELLED = "cancelled"


class OfferStatus(StrEnum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    PENDING = "pending"  # created; awaiting buyer confirmation / provider settlement
    SUCCEEDED = "succeeded"
    FAILED = (
        "failed"  # includes voided/cancelled charges — ponytail: one bucket, split if ops needs it
    )
    REFUNDED = "refunded"


class PayoutStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"  # transfer rejected/errored; admin retries via /v1/admin/payouts/{id}/retry


# ---------- Response views ----------


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    buyer_id: str
    service_type_id: str
    buyer_price: Decimal
    created_at: datetime
    expires_at: datetime


class BuyerJobView(BaseModel):
    """What a buyer sees. `seller_payout` deliberately omitted."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    quote_id: UUID
    buyer_id: str
    service_type_id: str
    buyer_price: Decimal
    seller_id: str | None
    status: JobStatus
    created_at: datetime
    accepted_at: datetime | None
    completed_at: datetime | None


class SellerJobView(BaseModel):
    """What a seller sees for an accepted/completed job. `buyer_price` omitted."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_type_id: str
    seller_id: str
    seller_payout: Decimal
    status: JobStatus
    created_at: datetime
    accepted_at: datetime | None
    completed_at: datetime | None


class SellerOfferView(BaseModel):
    """An offer directed at a seller. `buyer_price` omitted."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    service_type_id: str
    seller_id: str
    seller_payout: Decimal
    status: OfferStatus
    offered_at: datetime
    expires_at: datetime


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    buyer_price: Decimal
    seller_payout: Decimal
    margin: Decimal
    completed_at: datetime


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    seller_id: str
    rating: int
    comment: str | None
    created_at: datetime


class ServiceTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    base_buyer_price: Decimal
    base_seller_payout: Decimal


class SellerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tier: str
    capacity: int
    rating: float | None
    rating_count: int
    completed_jobs: int


class OnboardingOut(BaseModel):
    onboarding_url: str
    payments_ready: bool


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor: str
    action: str
    target: str
    created_at: datetime


class MarginSummaryOut(BaseModel):
    transactions: int
    gross_revenue: Decimal
    seller_payouts: Decimal
    platform_margin: Decimal
    take_rate: float


# ---------- Request bodies ----------
#
# Buyer/seller identity is NOT a body field: it comes from the authenticated
# principal (see `auth.py`). Accepting it here would let anyone act as anyone.


class QuoteRequest(BaseModel):
    service_type_id: str = Field(min_length=1, max_length=128)


class JobCreateRequest(BaseModel):
    quote_id: UUID


class AvailabilityRequest(BaseModel):
    service_type_id: str = Field(min_length=1, max_length=128)


class ReviewRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class SellerProfileUpdate(BaseModel):
    capacity: int = Field(ge=1, le=1000)


class AdminSellerBody(BaseModel):
    """Operator management of a seller: tier (drives payout multipliers) + capacity."""

    tier: str | None = Field(default=None, max_length=64)
    capacity: int | None = Field(default=None, ge=1, le=1000)


# ---------- Admin request bodies (validated at the trust boundary) ----------


class ServiceTypeBody(BaseModel):
    base_buyer_price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=12, decimal_places=2)
    base_seller_payout: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=12, decimal_places=2)


class PipelinesBody(BaseModel):
    buyer: list[str] = Field(default_factory=list[str], max_length=64)
    seller: list[str] = Field(default_factory=list[str], max_length=64)


class MarginFloorBody(BaseModel):
    absolute: Decimal = Field(
        default=Decimal(0), ge=0, allow_inf_nan=False, max_digits=12, decimal_places=2
    )
    pct: Decimal = Field(
        default=Decimal(0), ge=0, lt=1, allow_inf_nan=False, max_digits=5, decimal_places=4
    )
    ceiling_multiplier: Decimal = Field(
        default=Decimal(3), gt=0, allow_inf_nan=False, max_digits=6, decimal_places=2
    )


class MatchingStrategyBody(BaseModel):
    strategy: str = Field(min_length=1, max_length=64)
