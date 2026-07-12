"""Pricing pipeline composition tests (pure, no DB)."""

from datetime import UTC, datetime

import pytest

from marketplace.models import Side
from marketplace.pricing import PricingContext, register, run_pipeline


def _ctx(side: Side = Side.BUYER) -> PricingContext:
    return PricingContext(
        side=side,
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
    with pytest.raises(KeyError):
        run_pipeline(10.0, ["nope_does_not_exist"], _ctx())


def test_composition_order_matters() -> None:
    def _times2(price: float, ctx: PricingContext) -> float:
        del ctx
        return price * 2.0

    def _plus5(price: float, ctx: PricingContext) -> float:
        del ctx
        return price + 5.0

    register("test_times2")(_times2)
    register("test_plus5")(_plus5)

    assert run_pipeline(10.0, ["test_times2", "test_plus5"], _ctx()) == 25.0
    assert run_pipeline(10.0, ["test_plus5", "test_times2"], _ctx()) == 30.0


def test_per_adjuster_params_injected() -> None:
    def _read(price: float, ctx: PricingContext) -> float:
        return price * float(ctx.params.get("k", 1.0))

    register("test_param_reader")(_read)
    assert (
        run_pipeline(10.0, ["test_param_reader"], _ctx(), {"test_param_reader": {"k": 3.0}}) == 30.0
    )


def test_surge_at_supply_zero_uses_max() -> None:
    ctx = _ctx()
    ctx.live_supply = 0
    ctx.live_demand = 5
    out = run_pipeline(
        10.0, ["surge_by_demand_ratio"], ctx, {"surge_by_demand_ratio": {"max_multiplier": 3.0}}
    )
    assert out == 30.0


def test_surge_no_op_on_seller_side() -> None:
    ctx = _ctx(side=Side.SELLER)
    ctx.live_supply = 1
    ctx.live_demand = 100
    assert run_pipeline(7.0, ["surge_by_demand_ratio"], ctx) == 7.0


def test_new_buyer_discount_only_for_zero_completed() -> None:
    ctx_new = _ctx()
    assert ctx_new.buyer_completed_jobs == 0
    out_new = run_pipeline(
        10.0, ["new_buyer_discount"], ctx_new, {"new_buyer_discount": {"discount_pct": 0.2}}
    )
    assert out_new == 8.0

    ctx_returning = _ctx()
    ctx_returning.buyer_completed_jobs = 3
    out_returning = run_pipeline(
        10.0, ["new_buyer_discount"], ctx_returning, {"new_buyer_discount": {"discount_pct": 0.2}}
    )
    assert out_returning == 10.0


def test_bad_param_is_clamped() -> None:
    # discount_pct > 1 would drive price negative; the bounded reader clamps to [0, 1].
    ctx = _ctx()
    out = run_pipeline(
        10.0, ["new_buyer_discount"], ctx, {"new_buyer_discount": {"discount_pct": 5.0}}
    )
    assert out == 0.0  # clamped to 1.0 discount → price * 0
