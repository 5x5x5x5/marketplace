# marketplace

Two-sided marketplace where the platform sets the buyer-facing price and the seller payout **independently** and keeps the spread. Pricing pipelines, matching strategy, margin floor, and adjuster parameters are all tunable at runtime via the admin API — no deploy required.

## Mechanism

1. Seller posts availability for a service type.
2. Buyer requests a quote. The platform runs a buyer-side adjuster pipeline against a base price.
3. The platform probes seller-side payout against currently-available sellers; if the implied margin would fall below the configured floor, the buyer price is bumped up (or the quote is rejected if the bump exceeds a ceiling).
4. Buyer accepts → job is created → matching strategy picks one available seller and offers them the job at a payout computed by the seller-side pipeline.
5. Seller accepts, completes → transaction booked with `margin = buyer_price − seller_payout`.

Buyer- and seller-facing responses use distinct view models that exclude the other side's number. Only admin endpoints see both.

## Commands

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv run uvicorn marketplace.api:app --reload
```

## Endpoints

**Buyer**

| Method | Path             | Body                              | Returns       |
|--------|------------------|-----------------------------------|---------------|
| POST   | `/quotes`        | `{buyer_id, service_type_id}`     | `Quote`       |
| POST   | `/jobs`          | `{quote_id}`                      | `BuyerJobView` (no `seller_payout`) |
| GET    | `/jobs/{id}?role=buyer` | —                          | `BuyerJobView` |

**Seller**

| Method | Path                                            | Body                | Returns       |
|--------|-------------------------------------------------|---------------------|---------------|
| POST   | `/availability`                                 | `{seller_id, service_type_id}` | `{status: ok}` |
| DELETE | `/availability/{seller_id}/{service_type_id}`   | —                   | `{status: ok}` |
| GET    | `/jobs/offered?seller_id=...`                   | —                   | `list[SellerJobView]` (no `buyer_price`) |
| POST   | `/jobs/{id}/accept`                             | `{seller_id}`       | `{status: ok}` |
| POST   | `/jobs/{id}/complete`                           | `{seller_id}`       | `Transaction`  |

**Admin**

| Method | Path                                              | Body                                       |
|--------|---------------------------------------------------|--------------------------------------------|
| GET    | `/admin/config`                                   | —                                          |
| PUT    | `/admin/config/service_types/{id}`                | `{base_buyer_price, base_seller_payout}`   |
| PUT    | `/admin/config/pipelines/{service_type_id}`       | `{buyer: [...names], seller: [...names]}`  |
| PUT    | `/admin/config/margin_floor`                      | `{absolute, pct, ceiling_multiplier}`      |
| PUT    | `/admin/config/matching_strategy`                 | `{strategy: cheapest_payout / fifo / highest_rated}` |
| PUT    | `/admin/config/adjuster_params/{adjuster_name}`   | per-adjuster params dict                   |
| GET    | `/admin/transactions`                             | —                                          |
| GET    | `/admin/margins/summary`                          | —                                          |

## Built-in adjusters

All in `src/marketplace/pricing.py`. New adjusters are registered with the `@register("name")` decorator; nothing else changes.

| Name | Side | Params |
|------|------|--------|
| `surge_by_demand_ratio`   | buyer  | `max_multiplier`, `min_multiplier` |
| `time_of_day_multiplier`  | both   | `multipliers: dict[hour_str, float]` |
| `new_buyer_discount`      | buyer  | `discount_pct` |
| `supply_incentive`        | seller | `max_bonus_pct` |
| `seller_tier_multiplier`  | seller | `tiers: dict[tier_name, float]` |

## Built-in matching strategies

- `cheapest_payout` — picks the seller with the lowest projected payout that respects the margin floor (default).
- `fifo` — first available seller by `available_since`, subject to the floor.
- `highest_rated` — highest-rated seller, ties broken FIFO, subject to the floor.

## Out of scope (v1)

Payment, geo, real auth, persistence, real-time push, cancellation beyond status, seller bidding, ML pricing.
