# Fee-aware margin — design

**Date:** 2026-07-15
**Status:** Approved (source: estimate-on-the-charge; floor: fee-aware — both chosen 2026-07-15)

## Problem

The `Transaction` ledger records margin gross of provider fees. Stripe charges
~2.9% + 30¢ per captured charge and keeps the fee on refunds. Verified on the
test account: $50.00 of ledger margin landed as $42.14 cash ($7.86 in fees).
Two concrete failures:

1. **Reporting:** `GET /v1/admin/margins/summary` overstates real margin; a
   month-end reconciliation against the Stripe balance will not match.
2. **Pricing:** the margin floor is enforced on gross spread
   (`api.py` quote path, ~line 543), so a job priced exactly at the floor is
   guaranteed to *lose* money net of fees.
3. **The refund hole:** a job charged then cancelled/refunded never books a
   `Transaction` row, so its fee cost is invisible today.

## Decision summary

- **Fee source: estimate, stamped on the charge.** Fees attach to charges,
  not completions — `Payment` (1:1 with job, exists for every charge) carries
  a `fee_estimate` snapshot computed at charge creation. This closes the
  refund hole for free: a `REFUNDED` payment keeps its fee.
- **Floor: fee-aware.** The quote-time check requires
  `margin ≥ effective_floor(bp) + fee(bp)`, so floor-priced jobs net positive.
- **Not actual fees.** No balance-transaction reconciliation, no new provider
  surface, no fake-provider simulation. Estimate-vs-actual drift is pennies
  (international cards / currency variance); a fork wanting to-the-penny books
  layers reconciliation on top.

## Design

### 1. Fee config — two columns on the platform-config row

`PlatformConfig` (entities.py, the id=1 singleton via `repo.get_platform_config`)
gains:

- `fee_pct: Numeric(5, 4)`, default `Decimal("0.029")`
- `fee_fixed: _MONEY`, default `Decimal("0.30")`

Defaults are Stripe's standard card rate — the sensible template default.

A `FeeConfig` frozen dataclass (`config.py`, next to `MarginFloor`) carries
`pct: Decimal` and `fixed: Decimal`; `PricingConfig` gains a `fees: FeeConfig`
field and `repo` loads it from the row wherever it builds the snapshot.

A single pure helper in `matching.py` next to `effective_floor` (both are
money-boundary spread math), one definition used by both the quote and charge
paths:

```python
def estimated_fee(amount: Decimal, fees: FeeConfig) -> Decimal:
    return to_money(amount * fees.pct + fees.fixed)
```

Loading: a one-liner `repo.fee_config(session) -> FeeConfig` reads the row via
`get_platform_config`; the `PricingConfig` snapshot builder uses it, and the
charge site calls it directly.

### 2. Admin endpoint — `PUT /v1/admin/config/fees`

Follows `update_margin_floor` (api.py:1213) exactly: body
`FeesBody {pct: Decimal, fixed: Decimal}` with validation `0 ≤ pct < 1` and
`fixed ≥ 0` (Pydantic field constraints), mutates the config row, writes an
`audit(...)` row (`"update_fees"`, target `"platform"`), returns the stringified
values. The fee block also joins the `GET /v1/admin/config` dict:
`"fees": {"pct": ..., "fixed": ...}`.

### 3. Fee estimate stamped on the charge

`Payment` gains `fee_estimate: Mapped[Decimal] = mapped_column(_MONEY)`.
At the single charge-creation site (offer-accept, api.py ~line 1017), set
`fee_estimate=estimated_fee(job.buyer_price, repo.fee_config(session))` — a
snapshot of the config at charge time. Later config changes never rewrite stamped rows (same
immutability philosophy as `Transaction`).

The estimate is stamped unconditionally at creation; whether it *counts* is a
status question answered at read time (next section). `PaymentStatus.FAILED`
already buckets voided/cancelled charges, so "captured" = status in
`(SUCCEEDED, REFUNDED)` — a PI voided before capture costs Stripe-nothing and
is correctly excluded.

### 4. Margin summary goes net-of-fees

`MarginSummaryOut` gains two fields; `margins_summary` (api.py:1692) computes:

- `fees_estimated` = sum of `fee_estimate` over payments with status
  `SUCCEEDED` or `REFUNDED`
- `platform_margin_net_of_fees` = `platform_margin + adjustments_net −
  fees_estimated`

**Semantics — this is a cash view, documented on the endpoint:** the fee is
sunk the moment a charge captures. Fees on refunded jobs finally appear as the
loss they are, and fees on charged-but-in-flight jobs dip net until their
margin books at completion. This temporal mismatch is deliberate (it matches
the bank account, which is the point); do not "fix" it by filtering to
completed jobs.

### 5. Fee-aware floor at quote time

In the quote path (api.py ~543), the check becomes:

```python
required = effective_floor(buyer_price, cfg.margin_floor) + estimated_fee(buyer_price, cfg.fees)
if buyer_price - probe < required:
```

The bump keeps the existing anti-leak whole-unit rounding, then verifies —
because both the pct-branch floor and the fee grow with the bumped price, a
single ceil can undershoot:

```python
target = to_money(math.ceil(probe + required))
while target - probe < effective_floor(target, cfg.margin_floor) + estimated_fee(target, cfg.fees):
    target += 1  # converges in ≤2 steps: floor_pct + fee_pct ≪ 1
```

The `ceiling_multiplier` rejection is unchanged and checked against the final
`target`. Quote-time only; existing quotes/jobs are untouched.

### 6. Migration #9

One revision, three `ADD COLUMN`s, all with server defaults so existing rows
are valid:

- `payments.fee_estimate` — `server_default="0"` (pre-existing charges show
  zero fee; noted in docs, no backfill)
- `platform_config.fee_pct` — `server_default="0.029"`
- `platform_config.fee_fixed` — `server_default="0.30"`

Migration renders plain column types (no app-internal imports), matching the
existing convention. Chain: exactly 9 from scratch.

### 7. Demo + docs

- Demo: a step after the summary-touching acts asserting `fees_estimated > 0`
  and `platform_margin_net_of_fees == platform_margin + adjustments_net −
  fees_estimated` (exact Decimal equality via string round-trip).
- `ROADMAP.md`: item #2 → done (moved into the Done section, one paragraph).
- `README.md`: fees endpoint in the admin list; summary fields mentioned.
- `SECURITY.md`: note that `fee_estimate` is an estimate (not reconciled
  actuals) and that pre-migration rows carry 0.
- `CLAUDE.md`: migration count 8 → 9; a non-negotiable line: the floor check
  is net-of-fees — never compare the floor against gross spread.

## Testing

- `estimated_fee` unit math (quantization, zero-config = zero fee).
- Charge stamps the estimate from current config; config change afterwards
  does not rewrite it.
- Summary: refunded charge's fee still counted (the roadmap hole); FAILED/
  PENDING charges excluded; net math exact.
- Floor: quote priced at the floor nets ≥ 0 after fee; bump-verify loop
  boundary test (pct branch where one ceil undershoots); ceiling rejection
  still fires against the final target.
- Admin endpoint: validation 422s (pct = 1, negative fixed), audit row
  written, GET config shows the block.
- Both backends green; fresh-volume Postgres migration chain = 9.

## Non-goals

- Actual-fee reconciliation from Stripe balance transactions (fork work,
  layers on top of the estimate).
- Transfer/payout-side fees (Connect transfer pricing varies by account
  setup; fork-specific).
- Currency-specific or card-brand fee tables — one pct+fixed pair.
- Backfilling estimates for pre-migration payments.
- Any change to `Transaction` — the immutable margin ledger stays untouched.
