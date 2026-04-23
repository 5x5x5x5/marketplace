"""Pricing engine.

An adjuster is `(price, ctx) -> price`. A pipeline runs them in declared order.
Adjusters are registered into a global REGISTRY at import time and composed
from configuration (an ordered list of adjuster names per service-type, per
side). Adding an adjuster requires code; composing or tuning does not.

Each adjuster reads its parameters from `ctx.params`, which `run_pipeline`
injects from `Config.adjuster_params[<adjuster_name>]` before each call.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import BuyerProfile, SellerProfile, ServiceType, Side


@dataclass
class PricingContext:
    side: Side
    service_type: ServiceType
    buyer_id: str | None = None
    seller_id: str | None = None
    buyer_profile: BuyerProfile | None = None
    seller_profile: SellerProfile | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    live_supply: int = 0
    live_demand: int = 0
    params: dict[str, Any] = field(default_factory=dict[str, Any])


Adjuster = Callable[[float, PricingContext], float]

REGISTRY: dict[str, Adjuster] = {}


def register(name: str) -> Callable[[Adjuster], Adjuster]:
    """Register an adjuster under `name`. Idempotent: re-registering overwrites."""

    def wrap(fn: Adjuster) -> Adjuster:
        REGISTRY[name] = fn
        return fn

    return wrap


def run_pipeline(
    base: float,
    adjuster_names: list[str],
    ctx: PricingContext,
    params_by_adjuster: dict[str, dict[str, Any]] | None = None,
) -> float:
    """Apply adjusters in order. Mutates only `ctx.params` between adjusters."""
    price = base
    params_by_adjuster = params_by_adjuster or {}
    for name in adjuster_names:
        if name not in REGISTRY:
            raise KeyError(f"unknown adjuster: {name!r}")
        ctx.params = params_by_adjuster.get(name, {})
        price = REGISTRY[name](price, ctx)
    return price


# ---------- Built-in adjusters ----------


@register("surge_by_demand_ratio")
def surge_by_demand_ratio(price: float, ctx: PricingContext) -> float:
    """Buyer-side: scale price linearly between min/max multipliers based on
    the live demand/supply ratio. No-op on seller side.

    Params:
        max_multiplier: float (default 2.5) — applied at ratio >= 2.0 or zero supply
        min_multiplier: float (default 1.0) — applied at ratio <= 1.0
    """
    if ctx.side != Side.BUYER:
        return price
    max_mult = float(ctx.params.get("max_multiplier", 2.5))
    min_mult = float(ctx.params.get("min_multiplier", 1.0))
    if ctx.live_supply == 0:
        return price * max_mult
    ratio = ctx.live_demand / ctx.live_supply
    if ratio <= 1.0:
        mult = min_mult
    elif ratio >= 2.0:
        mult = max_mult
    else:
        mult = min_mult + (max_mult - min_mult) * (ratio - 1.0)
    return price * mult


@register("time_of_day_multiplier")
def time_of_day_multiplier(price: float, ctx: PricingContext) -> float:
    """Either side. Multiplier per hour-of-day (0-23, as string keys in JSON).

    Params:
        multipliers: dict[str, float] — e.g. {"17": 1.5, "18": 1.5}
    """
    table = ctx.params.get("multipliers", {})
    mult = float(table.get(str(ctx.now.hour), 1.0))
    return price * mult


@register("new_buyer_discount")
def new_buyer_discount(price: float, ctx: PricingContext) -> float:
    """Buyer-side discount when buyer has zero completed jobs.

    Params:
        discount_pct: float in [0, 1] — fraction off (default 0.10)
    """
    if ctx.side != Side.BUYER:
        return price
    if ctx.buyer_profile is None or ctx.buyer_profile.completed_jobs > 0:
        return price
    discount = float(ctx.params.get("discount_pct", 0.10))
    return price * (1.0 - discount)


@register("supply_incentive")
def supply_incentive(price: float, ctx: PricingContext) -> float:
    """Seller-side bonus when supply is tight (demand > supply).

    Params:
        max_bonus_pct: float — bonus cap as a fraction (default 0.5 → +50%)
    """
    if ctx.side != Side.SELLER:
        return price
    if ctx.live_supply == 0 or ctx.live_demand <= ctx.live_supply:
        return price
    ratio = ctx.live_demand / ctx.live_supply
    max_bonus = float(ctx.params.get("max_bonus_pct", 0.5))
    bonus = min(max_bonus, max_bonus * (ratio - 1.0))
    return price * (1.0 + bonus)


@register("seller_tier_multiplier")
def seller_tier_multiplier(price: float, ctx: PricingContext) -> float:
    """Seller-side multiplier by seller tier.

    Params:
        tiers: dict[str, float] — e.g. {"premium": 1.2, "standard": 1.0, "new": 0.9}
    """
    if ctx.side != Side.SELLER or ctx.seller_profile is None:
        return price
    table = ctx.params.get("tiers", {})
    mult = float(table.get(ctx.seller_profile.tier, 1.0))
    return price * mult
