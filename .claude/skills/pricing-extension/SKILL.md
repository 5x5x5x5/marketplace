---
name: pricing-extension
description: Add a new pricing adjuster (buyer price or seller payout rule) or a new matching strategy (how an offer picks a seller) to this marketplace, keep the pricing/matching core pure, test it, and enable it through the admin config API. Use when asked for a new surge/discount/bonus/tier rule, a new way of choosing sellers, or when editing src/marketplace/pricing.py or matching.py.
---

# Adding an adjuster or a matching strategy

Buyer price and seller payout are computed independently by two pipelines;
the platform keeps the spread. Adding a rule is code (a registered function);
composing and tuning rules is configuration that operators change at runtime
through admin endpoints. Keep that split: never hard-code a pipeline or a
parameter value an operator should own.

## The purity rule

`pricing.py` and `matching.py` take only the snapshots in `config.py`
(`PricingConfig`, `Candidate`) and a `PricingContext`. No `Session`, no ORM
rows, no `datetime.now()` inside an adjuster (use `ctx.now`), no `Decimal`
inside the pipeline (prices are `float` ratios there; money is quantized with
`models.to_money` at the boundary). This is what lets `tests/test_pricing.py`
and `tests/test_matching.py` run without a database and makes a quote
reproducible from its inputs.

If the rule needs a fact the context doesn't carry, add a field to
`PricingContext` (or `Candidate`) with a neutral default, load it in
`repo.py`, and fill it at **both** buyer-side construction sites:
`api.create_quote` and `catalog.indicative_buyer_price` (the public "from"
price; it has no buyer, so it uses the neutral value). The two carry a
cross-reference comment because they have drifted before.

## Adjuster

```python
@register("weekend_multiplier")
def weekend_multiplier(price: float, ctx: PricingContext) -> float:
    """Buyer-side: multiply on Saturday/Sunday.

    Params:
        multiplier: float (default 1.2), clamped to [0, 10]
    """
    if ctx.side != Side.BUYER:
        return price
    if ctx.now.weekday() < 5:
        return price
    return price * _bounded(ctx.params.get("multiplier"), 1.2, 0.0, 10.0)
```

- Check `ctx.side` first: the same registry serves both pipelines and an
  operator can put any name in either list.
- Read every parameter through `_bounded(value, default, lo, hi)`. Params are
  free-form JSON from an admin endpoint; an unbounded value (negative, huge,
  NaN) would drive prices negative or infinite. Bounds are enforced where the
  value is used, not by a validation model.
- Document the params in the docstring; the README's "Built-in adjusters"
  table is maintained by hand from these, so add a row there too.

## Strategy

Signature: `(buyer_price: Decimal, candidates: list[Candidate], cfg, supply, demand) -> MatchResult | None`.

- Price candidates with `_priced(...)`, which calls `seller_payout_for` (the
  single payout computation, shared with the quote probe) and filters with
  `passes_floor`. That filter is **net of fees**
  (`required_spread = floor + estimated provider fee`); never compare the gross
  spread, or the platform books a match that loses money after the
  processor's cut.
- Return `None` when nothing clears the floor; the caller handles it.
- Break ties deterministically (the built-ins use `available_since`), so a
  re-run with the same inputs picks the same seller.
- Candidates arrive already filtered for capacity and for sellers who were
  already offered this job; don't re-filter, and don't touch pinned jobs
  (they never re-match).

## Tests (write them first)

Pure unit tests, no DB, in `tests/test_pricing.py` / `tests/test_matching.py`,
following the existing helpers there (`_ctx(...)` with a fixed `now`):
the rule fires, it is a no-op on the other side, a bad param is clamped, and
for a strategy, the floor (fee-aware) excludes a candidate. Then one API
test that enables it through the admin endpoint and quotes.

## Enabling it (operators, no deploy)

```bash
PUT /v1/admin/config/pipelines/{service_type_id}   {"buyer": ["surge_by_demand_ratio", "weekend_multiplier"], "seller": []}
PUT /v1/admin/config/adjuster_params/weekend_multiplier   {"multiplier": 1.3}
PUT /v1/admin/config/matching_strategy               {"strategy": "<name>"}
```

Unknown adjuster names get 422, unknown strategies 422, unknown params 404;
each change is written to the audit log. Pipelines are ordered: adjusters
run left to right on the running price.

## Done

Gate green (`gate` skill), README tables updated, and the margin floor still
enforced at both sites (quote-time probe and match time). If the new rule
changes what a buyer sees before the floor bump, check that
`catalog.indicative_buyer_price` still agrees with the quote path.
