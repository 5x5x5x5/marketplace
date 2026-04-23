"""Pricing pipeline composition tests.

The order of adjusters in the pipeline determines the result. The same set of
adjusters applied in different orders should produce different prices, and the
test below makes that explicit.
"""

from datetime import UTC, datetime

from marketplace.models import BuyerProfile, ServiceType, Side
from marketplace.pricing import PricingContext, register, run_pipeline


def _ctx(side: Side = Side.BUYER) -> PricingContext:
    return PricingContext(
        side=side,
        service_type=ServiceType(id="t", base_buyer_price=10.0, base_seller_payout=7.0),
        buyer_profile=BuyerProfile(id="b1"),
        live_supply=2,
        live_demand=2,
        now=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
    )


def test_empty_pipeline_returns_base() -> None:
    assert run_pipeline(10.0, [], _ctx()) == 10.0


def test_single_adjuster_applied() -> None:
    def _double(price: float, ctx: PricingContext) -> float:
        del ctx
        return price * 2.0

    register("test_double")(_double)
    assert run_pipeline(10.0, ["test_double"], _ctx()) == 20.0


def test_unknown_adjuster_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        run_pipeline(10.0, ["nope_does_not_exist"], _ctx())


def test_pipeline_composition_order_matters() -> None:
    """Adjuster A multiplies, B adds. (10 * 2) + 5 != (10 + 5) * 2."""

    def _times2(price: float, ctx: PricingContext) -> float:
        del ctx
        return price * 2.0

    def _plus5(price: float, ctx: PricingContext) -> float:
        del ctx
        return price + 5.0

    register("test_times2")(_times2)
    register("test_plus5")(_plus5)

    a_then_b = run_pipeline(10.0, ["test_times2", "test_plus5"], _ctx())
    b_then_a = run_pipeline(10.0, ["test_plus5", "test_times2"], _ctx())
    assert a_then_b == 25.0  # (10 * 2) + 5
    assert b_then_a == 30.0  # (10 + 5) * 2
    assert a_then_b != b_then_a


def test_per_adjuster_params_injected() -> None:
    """Each adjuster sees only its own params, looked up by name."""

    def _read(price: float, ctx: PricingContext) -> float:
        return price * float(ctx.params.get("k", 1.0))

    register("test_param_reader")(_read)
    params = {"test_param_reader": {"k": 3.0}}
    assert run_pipeline(10.0, ["test_param_reader"], _ctx(), params) == 30.0


def test_surge_by_demand_ratio_at_supply_zero_uses_max() -> None:
    ctx = _ctx()
    ctx.live_supply = 0
    ctx.live_demand = 5
    out = run_pipeline(
        10.0,
        ["surge_by_demand_ratio"],
        ctx,
        {"surge_by_demand_ratio": {"max_multiplier": 3.0}},
    )
    assert out == 30.0


def test_surge_no_op_on_seller_side() -> None:
    ctx = _ctx(side=Side.SELLER)
    ctx.live_supply = 1
    ctx.live_demand = 100
    out = run_pipeline(7.0, ["surge_by_demand_ratio"], ctx)
    assert out == 7.0


def test_new_buyer_discount_only_for_zero_completed() -> None:
    ctx_new = _ctx()
    assert ctx_new.buyer_profile is not None
    assert ctx_new.buyer_profile.completed_jobs == 0

    out_new = run_pipeline(
        10.0, ["new_buyer_discount"], ctx_new, {"new_buyer_discount": {"discount_pct": 0.2}}
    )
    assert out_new == 8.0

    ctx_returning = _ctx()
    ctx_returning.buyer_profile = BuyerProfile(id="b2", completed_jobs=3)
    out_returning = run_pipeline(
        10.0,
        ["new_buyer_discount"],
        ctx_returning,
        {"new_buyer_discount": {"discount_pct": 0.2}},
    )
    assert out_returning == 10.0
