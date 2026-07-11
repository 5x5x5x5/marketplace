"""Pydantic models for the marketplace.

Two-sided concept: a `Job` ties a buyer-side `Quote` (the price the buyer agreed
to) to a seller-side payout (the amount the seller will receive). The platform
keeps the spread. Buyer- and seller-facing views deliberately omit the other
side's number — see `BuyerJobView` and `SellerJobView`.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class JobStatus(StrEnum):
    QUOTED = "quoted"
    MATCHED = "matched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# ---------- Core entities ----------


class ServiceType(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    base_buyer_price: float = Field(gt=0, allow_inf_nan=False)
    base_seller_payout: float = Field(gt=0, allow_inf_nan=False)


class Quote(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    buyer_id: str
    service_type_id: str
    buyer_price: float
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Job(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    quote_id: UUID
    buyer_id: str
    service_type_id: str
    buyer_price: float
    seller_id: str | None = None
    seller_payout: float | None = None
    status: JobStatus = JobStatus.QUOTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Transaction(BaseModel):
    job_id: UUID
    buyer_price: float
    seller_payout: float
    margin: float
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------- Profiles (used by adjusters and matching) ----------


class SellerProfile(BaseModel):
    id: str
    tier: str = "standard"
    rating: float = Field(default=4.0, ge=0, le=5)
    completed_jobs: int = 0


class BuyerProfile(BaseModel):
    id: str
    completed_jobs: int = 0


class AvailabilityRecord(BaseModel):
    seller_id: str
    service_type_id: str
    since: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------- Side-specific job views (information asymmetry) ----------


class BuyerJobView(BaseModel):
    """What a buyer sees. `seller_payout` deliberately omitted."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    quote_id: UUID
    buyer_id: str
    service_type_id: str
    buyer_price: float
    seller_id: str | None
    status: JobStatus
    created_at: datetime

    @classmethod
    def from_job(cls, job: Job) -> "BuyerJobView":
        return cls(
            id=job.id,
            quote_id=job.quote_id,
            buyer_id=job.buyer_id,
            service_type_id=job.service_type_id,
            buyer_price=job.buyer_price,
            seller_id=job.seller_id,
            status=job.status,
            created_at=job.created_at,
        )


class SellerJobView(BaseModel):
    """What a seller sees. `buyer_price` deliberately omitted."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    quote_id: UUID
    service_type_id: str
    seller_id: str
    seller_payout: float
    status: JobStatus
    created_at: datetime

    @classmethod
    def from_job(cls, job: Job) -> "SellerJobView":
        if job.seller_id is None or job.seller_payout is None:
            raise ValueError("Job has no seller assigned")
        return cls(
            id=job.id,
            quote_id=job.quote_id,
            service_type_id=job.service_type_id,
            seller_id=job.seller_id,
            seller_payout=job.seller_payout,
            status=job.status,
            created_at=job.created_at,
        )


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


# ---------- Admin request bodies (validated at the trust boundary) ----------


class ServiceTypeBody(BaseModel):
    base_buyer_price: float = Field(gt=0, allow_inf_nan=False)
    base_seller_payout: float = Field(gt=0, allow_inf_nan=False)


class PipelinesBody(BaseModel):
    buyer: list[str] = Field(default_factory=list[str], max_length=64)
    seller: list[str] = Field(default_factory=list[str], max_length=64)


class MarginFloorBody(BaseModel):
    absolute: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    pct: float = Field(default=0.0, ge=0, lt=1, allow_inf_nan=False)
    ceiling_multiplier: float = Field(default=3.0, gt=0, allow_inf_nan=False)


class MatchingStrategyBody(BaseModel):
    strategy: str = Field(min_length=1, max_length=64)
