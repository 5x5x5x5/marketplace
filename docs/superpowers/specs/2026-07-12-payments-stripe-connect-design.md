# Payments & payouts via Stripe Connect — design

**Date:** 2026-07-12 · **Branch:** `payments-stripe-connect` · **Status:** approved

## Goal

Add real money movement to the marketplace template: buyer charges, escrow,
seller payouts, refunds, and seller onboarding/KYC — while keeping the template
runnable with **no Stripe account** (fake provider for dev/tests) and keeping
the spread model in our domain, not the provider's.

## Decisions (confirmed with Danny)

1. **Escrow model:** charge the buyer in full when a seller accepts; funds sit
   on the platform balance; transfer the seller payout on completion; refund on
   cancel. (Stripe "separate charges & transfers" — no auth-expiry ceiling on
   long jobs.)
2. **Lifecycle coupling:** new `AWAITING_PAYMENT` job state between seller
   accept and `ACCEPTED`. Work starts only once money is secured.
3. **Idempotency:** client-facing `Idempotency-Key` header ships now, plus
   (non-negotiable) webhook event dedup and idempotency keys on all outbound
   provider calls.
4. **Architecture:** provider port + two adapters — `FakeProvider`
   (deterministic, in-memory, dev/tests) and `StripeProvider` (official SDK).
   `STRIPE_SECRET_KEY` set → Stripe; unset → fake.

**Explicitly not** Stripe's `application_fee_amount` commission model. Charge
`buyer_price`, later transfer `seller_payout`; the platform balance retains the
spread by construction. The port has no "fee" concept — the spread stays in our
pricing core, which is the template's differentiator.

## Architecture

New package `src/marketplace/payments/`:

- **`port.py`** — `PaymentProvider` protocol + frozen result dataclasses.
  Methods: `create_seller_account`, `onboarding_link`, `charge_buyer`,
  `refund`, `transfer_to_seller`, `parse_webhook` (→ normalized
  `PaymentEvent`). Plus `to_minor_units(Decimal) -> int` (Stripe speaks integer
  cents; our `Decimal` stays canonical). Single currency via `CURRENCY=usd`
  setting.
- **`fake.py`** — `FakeProvider`: in-memory, deterministic. Charges return
  `succeeded` by default so dev/tests flow instantly; tests script
  `next_charge_status = "pending" | "failed"` to exercise the async path.
  Accepts unsigned webhook JSON (never selected in prod, by construction).
- **`stripe.py`** — `StripeProvider`: official `stripe` SDK (new dependency),
  **controller-properties accounts** (current Stripe guidance — the legacy
  Standard/Express/Custom split is deprecated), PaymentIntents, Transfers,
  `Webhook.construct_event` signature verification.

Selection: `get_provider()` FastAPI dependency reads settings once.

## Data model (Alembic migration #2)

| Table | Purpose | Key fields |
|---|---|---|
| `Payment` | buyer charge, 1:1 job | `job_id` unique, `buyer_id`, `amount` Numeric(12,2), status `pending/succeeded/failed/refunded`, `provider`, `provider_payment_id`, `client_secret` |
| `Payout` | seller transfer, 1:1 job | `job_id` unique, `seller_id`, `amount`, status `pending/paid/failed`, `provider_transfer_id` |
| `WebhookEvent` | dedup ledger | `provider_event_id` unique, `kind`, `received_at` |
| `IdempotencyKey` | client replay | (`principal`, `key`) unique, `path`, response status + body JSON |

Existing tables: `SellerProfile` += `provider_account_id`, `payments_ready`
(bool). `Job.status` += `AWAITING_PAYMENT`. `Transaction` stays the **ledger**
(booked margin at completion); `Payment`/`Payout` record actual money movement
— ledger and cash are different facts.

## Lifecycle integration

**Onboarding.** `POST /v1/seller/payments/onboard` → creates provider account
if none, returns onboarding URL. `account.updated` webhook (payouts enabled) →
`payments_ready = True`; fake is ready instantly. **Matching filters on
`payments_ready`** — an unonboarded seller is never offered a job. Test helpers
onboard in one line.

**Accept.** After the existing capacity check, same row lock:

1. Create `Payment` row + `provider.charge_buyer(...)` (outbound key
   `charge:{job_id}`).
2. Offer → `ACCEPTED`, job → `AWAITING_PAYMENT` (counts against seller
   capacity — they've committed).
3. Fake returns `succeeded` → job flows straight to `ACCEPTED` inline. Real
   Stripe → buyer polls `GET /v1/jobs/{id}` (`BuyerJobView` gains
   `payment_status` + `client_secret` while awaiting), confirms client-side,
   webhook `payment_intent.succeeded` → `ACCEPTED`.
4. Payment failure → stays `AWAITING_PAYMENT` for retry. Retry reuses the
   same `Payment` row and the same PaymentIntent (Stripe PIs accept a new
   payment method on retry; status returns to `pending`) — the 1:1
   job↔payment constraint holds. The existing lazy sweep gains a
   payment-timeout rule (`PAYMENT_TTL_MINUTES`): overdue → job `EXPIRED`,
   capacity freed, PI cancelled.

**Complete.** Requires `ACCEPTED` (payment succeeded by construction). Books
the `Transaction` ledger row as today, **plus** `Payout` row +
`provider.transfer_to_seller` (key `transfer:{job_id}`). Transfer failure →
`Payout.status = failed` + `POST /v1/admin/payouts/{id}/retry`.

**Cancel.** Buyer: `PENDING` (unchanged, no money) and `AWAITING_PAYMENT`
(cancel PI; refund in the rare succeeded-race). Admin cancel of `ACCEPTED` →
full refund (key `refund:{job_id}`), no seller transfer. Partial
refunds/disputes deferred.

**Webhook endpoint.** `POST /v1/payments/webhook` — unauthenticated,
signature-verified, dedup on `provider_event_id` (dup → 200 no-op, bad
signature → 400). Dispatches: payment succeeded/failed, account updated,
transfer paid/failed.

## Client idempotency

Optional `Idempotency-Key` header on money-mutating POSTs (accept, complete,
cancel, create-job) via a FastAPI dependency: first call stores (principal,
key, path, response); replay returns the stored response; same key + different
path → 409.

## Error handling

- Provider call fails mid-accept → 502, DB transaction rolls back, offer stays
  acceptable; outbound idempotency keys make the retry safe.
- No orphaned PIs: a retried accept reuses the outbound key `charge:{job_id}`
  and receives the *same* PaymentIntent back from the provider — the
  provider-succeeded/commit-failed case self-heals on retry rather than
  accumulating strays.
- Webhooks never 500 on duplicates; bad signature → 400.

## Testing

All against `FakeProvider`; Postgres-gated race tests unchanged:

- Happy path: accept → awaiting → webhook → accepted → complete → payout.
- Scripted payment failure + retry; payment timeout frees capacity.
- Cancel/refund paths; payout failure + admin retry.
- Onboarding gates matching; webhook dedup (same event twice); signature
  rejection (StripeProvider unit test with a constructed signature).
- Idempotency replay + conflict; Decimal → minor-units round-trip.

## Deferred

Disputes/chargebacks · partial refunds · multi-currency · payout schedules ·
checkout frontend (Stripe Elements/Checkout is the fork's job) · notifications
on payment events (ROADMAP #3).

## Constraints carried forward

Fork-agnostic template · pyright strict · SQLite local tests / Postgres prod ·
identity from authenticated principal only · ORM never leaves the API layer ·
pricing/matching core stays pure (payments never touch it).
