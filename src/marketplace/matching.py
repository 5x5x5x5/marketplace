"""Matching strategies.

A strategy receives the already-quoted Job (with `buyer_price` set), a list of
seller candidates, and the live config. It returns a `MatchResult` (chosen
seller_id and the seller_payout that the seller pipeline produced for them) or
`None` if no candidate satisfies the platform's margin floor.

Strategies are registered into a global STRATEGIES table; the operator selects
the active strategy at runtime via `Config.matching_strategy`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Config
from .models import Job, SellerProfile, Side
from .pricing import PricingContext, run_pipeline


@dataclass
class SellerCandidate:
    seller_id: str
    profile: SellerProfile
    available_since: datetime


@dataclass
class MatchResult:
    seller_id: str
    seller_payout: float


Strategy = Callable[
    [Job, list[SellerCandidate], Config, dict[str, Any]],
    MatchResult | None,
]

STRATEGIES: dict[str, Strategy] = {}


def register_strategy(name: str) -> Callable[[Strategy], Strategy]:
    def wrap(fn: Strategy) -> Strategy:
        STRATEGIES[name] = fn
        return fn

    return wrap


def _payout_for(
    candidate: SellerCandidate, job: Job, config: Config, supply: int, demand: int
) -> float:
    service_type = config.service_types[job.service_type_id]
    pipelines = config.get_pipelines(job.service_type_id)
    ctx = PricingContext(
        side=Side.SELLER,
        service_type=service_type,
        buyer_id=job.buyer_id,
        seller_id=candidate.seller_id,
        seller_profile=candidate.profile,
        live_supply=supply,
        live_demand=demand,
    )
    return run_pipeline(
        service_type.base_seller_payout,
        pipelines.seller,
        ctx,
        config.adjuster_params,
    )


def _passes_floor(buyer_price: float, payout: float, config: Config) -> bool:
    floor = max(config.margin_floor.absolute, config.margin_floor.pct * buyer_price)
    return (buyer_price - payout) >= floor


def _supply_demand(extras: dict[str, Any], candidates: list[SellerCandidate]) -> tuple[int, int]:
    return int(extras.get("supply", len(candidates))), int(extras.get("demand", 1))


@register_strategy("cheapest_payout")
def cheapest_payout(
    job: Job,
    candidates: list[SellerCandidate],
    config: Config,
    extras: dict[str, Any],
) -> MatchResult | None:
    """Lowest payout that still respects the margin floor."""
    supply, demand = _supply_demand(extras, candidates)
    valid: list[tuple[SellerCandidate, float]] = []
    for c in candidates:
        p = _payout_for(c, job, config, supply, demand)
        if _passes_floor(job.buyer_price, p, config):
            valid.append((c, p))
    if not valid:
        return None
    valid.sort(key=lambda cp: (cp[1], cp[0].available_since))
    chosen, payout = valid[0]
    return MatchResult(seller_id=chosen.seller_id, seller_payout=payout)


@register_strategy("fifo")
def fifo(
    job: Job,
    candidates: list[SellerCandidate],
    config: Config,
    extras: dict[str, Any],
) -> MatchResult | None:
    """First seller (by `available_since`) whose payout passes the floor."""
    supply, demand = _supply_demand(extras, candidates)
    for c in sorted(candidates, key=lambda c: c.available_since):
        p = _payout_for(c, job, config, supply, demand)
        if _passes_floor(job.buyer_price, p, config):
            return MatchResult(seller_id=c.seller_id, seller_payout=p)
    return None


@register_strategy("highest_rated")
def highest_rated(
    job: Job,
    candidates: list[SellerCandidate],
    config: Config,
    extras: dict[str, Any],
) -> MatchResult | None:
    """Highest-rated seller whose payout passes the floor; ties broken by FIFO."""
    supply, demand = _supply_demand(extras, candidates)
    for c in sorted(candidates, key=lambda c: (-c.profile.rating, c.available_since)):
        p = _payout_for(c, job, config, supply, demand)
        if _passes_floor(job.buyer_price, p, config):
            return MatchResult(seller_id=c.seller_id, seller_payout=p)
    return None
