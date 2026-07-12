"""Pricing/matching snapshots.

The pricing engine and matching strategies operate on these plain dataclasses,
never on ORM rows or the DB session — so they stay pure and testable. The API
layer loads a `PricingConfig` and a list of `Candidate`s from the database (see
`repo.py`) and hands them to the pure core.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class MarginFloor:
    """Platform's minimum spread per matched pair.

    Effective floor for a quote = max(absolute, pct * buyer_price). The
    `ceiling_multiplier` caps how far a floor-correcting bump can push the buyer
    price before the quote is rejected.
    """

    absolute: Decimal = Decimal(0)
    pct: Decimal = Decimal(0)
    ceiling_multiplier: Decimal = Decimal(3)


@dataclass
class ServiceSpec:
    id: str
    base_buyer_price: Decimal
    base_seller_payout: Decimal


@dataclass
class PricingConfig:
    service: ServiceSpec
    buyer_pipeline: list[str]
    seller_pipeline: list[str]
    adjuster_params: dict[str, dict[str, Any]]
    margin_floor: MarginFloor
    matching_strategy: str


@dataclass
class Candidate:
    """An available seller, with the facts pricing/matching need."""

    seller_id: str
    tier: str
    rating: float | None
    completed_jobs: int
    available_since: datetime
    active_jobs: int
    capacity: int

    @property
    def has_capacity(self) -> bool:
        return self.active_jobs < self.capacity
