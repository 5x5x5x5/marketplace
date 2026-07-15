"""Matching strategies (pure).

A strategy receives the quoted `buyer_price`, a list of eligible seller
`Candidate`s (already filtered to those with spare capacity and not previously
declined for this job), and the `PricingConfig`. It returns a `MatchResult`
(chosen seller_id + the Decimal payout the seller pipeline produced) or `None`
if no candidate satisfies the platform's margin floor.

Strategies are registered into a global STRATEGIES table; the operator selects
the active one via config. `seller_payout_for` is the single payout computation,
shared with the quote probe in `api.py`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from .config import Candidate, FeeConfig, MarginFloor, PricingConfig
from .models import Side, to_money
from .pricing import PricingContext, run_pipeline


@dataclass
class MatchResult:
    seller_id: str
    seller_payout: Decimal


Strategy = Callable[[Decimal, list[Candidate], PricingConfig, int, int], MatchResult | None]

STRATEGIES: dict[str, Strategy] = {}


def register_strategy(name: str) -> Callable[[Strategy], Strategy]:
    def wrap(fn: Strategy) -> Strategy:
        STRATEGIES[name] = fn
        return fn

    return wrap


def seller_payout_for(
    candidate: Candidate, cfg: PricingConfig, supply: int, demand: int
) -> Decimal:
    """The one place a seller's payout is computed. Returns a 2-dp Decimal."""
    ctx = PricingContext(
        side=Side.SELLER,
        seller_tier=candidate.tier,
        live_supply=supply,
        live_demand=demand,
    )
    raw = run_pipeline(
        float(cfg.service.base_seller_payout), cfg.seller_pipeline, ctx, cfg.adjuster_params
    )
    return to_money(raw)


def effective_floor(buyer_price: Decimal, floor: MarginFloor) -> Decimal:
    return max(floor.absolute, to_money(floor.pct * buyer_price))


def estimated_fee(amount: Decimal, fees: FeeConfig) -> Decimal:
    return to_money(amount * fees.pct + fees.fixed)


def required_spread(buyer_price: Decimal, floor: MarginFloor, fees: FeeConfig) -> Decimal:
    """Minimum spread that nets positive: the floor plus the provider's cut."""
    return effective_floor(buyer_price, floor) + estimated_fee(buyer_price, fees)


def passes_floor(buyer_price: Decimal, payout: Decimal, floor: MarginFloor) -> bool:
    return (buyer_price - payout) >= effective_floor(buyer_price, floor)


def _priced(
    candidates: list[Candidate], buyer_price: Decimal, cfg: PricingConfig, supply: int, demand: int
) -> list[tuple[Candidate, Decimal]]:
    """(candidate, payout) pairs that clear the margin floor."""
    out: list[tuple[Candidate, Decimal]] = []
    for c in candidates:
        payout = seller_payout_for(c, cfg, supply, demand)
        if passes_floor(buyer_price, payout, cfg.margin_floor):
            out.append((c, payout))
    return out


@register_strategy("cheapest_payout")
def cheapest_payout(
    buyer_price: Decimal, candidates: list[Candidate], cfg: PricingConfig, supply: int, demand: int
) -> MatchResult | None:
    """Lowest payout that still respects the margin floor."""
    valid = _priced(candidates, buyer_price, cfg, supply, demand)
    if not valid:
        return None
    valid.sort(key=lambda cp: (cp[1], cp[0].available_since))
    chosen, payout = valid[0]
    return MatchResult(seller_id=chosen.seller_id, seller_payout=payout)


@register_strategy("fifo")
def fifo(
    buyer_price: Decimal, candidates: list[Candidate], cfg: PricingConfig, supply: int, demand: int
) -> MatchResult | None:
    """First seller (by `available_since`) whose payout passes the floor."""
    valid = _priced(candidates, buyer_price, cfg, supply, demand)
    if not valid:
        return None
    valid.sort(key=lambda cp: cp[0].available_since)
    chosen, payout = valid[0]
    return MatchResult(seller_id=chosen.seller_id, seller_payout=payout)


@register_strategy("highest_rated")
def highest_rated(
    buyer_price: Decimal, candidates: list[Candidate], cfg: PricingConfig, supply: int, demand: int
) -> MatchResult | None:
    """Highest-rated seller whose payout passes the floor; ties broken by FIFO.

    An unrated seller (rating None) sorts below any rated one.
    """
    valid = _priced(candidates, buyer_price, cfg, supply, demand)
    if not valid:
        return None
    valid.sort(key=lambda cp: (-(cp[0].rating or -1.0), cp[0].available_since))
    chosen, payout = valid[0]
    return MatchResult(seller_id=chosen.seller_id, seller_payout=payout)
