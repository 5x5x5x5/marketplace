# Disputes + partial refunds — design

**Date:** 2026-07-14 · **Status:** approved · **Branch:** `disputes`

First sub-phase of the trust & safety bucket (chosen order: disputes →
seller→buyer reviews → moderation/abuse → notification preferences; each gets
its own spec/plan cycle).

## Goal

Resolve money disagreements without corrupting the ledger. Buyers dispute
completed jobs; admins arbitrate with independent partial refunds and payout
clawbacks; Stripe chargebacks are recorded truthfully. The escrow model means
the platform holds the money in every disagreement — this phase is where the
template's differentiator survives contact with conflict.

## Decisions (maintainer-confirmed)

1. **Eligibility:** completed jobs only, within `DISPUTE_WINDOW_DAYS`
   (setting, default 7) of `completed_at`; one dispute per job. Pre-completion
   problems keep using the cancel path.
2. **Resolution:** admin sets `refund_amount` (0..buyer_price, partial Stripe
   refund to the buyer) and `clawback_amount` (0..seller_payout, partial
   transfer reversal from the seller) **independently**; the platform absorbs
   or gains the difference. `0/0` = rejected. Covers goodwill refunds,
   seller-fault clawbacks, and everything between.
3. **Chargebacks:** `charge.dispute.created/closed` map into the same
   `disputes` table with `source=provider`; record + notify admins; evidence
   submission stays in the Stripe dashboard (fork work).
4. **Ledger:** append-only `adjustments` rows; `Transaction` rows stay
   immutable. Margin summary reports gross AND net-of-adjustments.

## Data model (Alembic migration #5)

| Table | Fields |
|---|---|
| `disputes` | `id` UUID pk · `job_id` FK **unique** · `source` enum buyer/provider · `buyer_id` String(128) · `reason` String(2000) · `status` enum open/resolved/chargeback_won/chargeback_lost (non-native, as elsewhere) · `refund_amount`/`clawback_amount` Numeric(12,2) nullable (null until resolved; 0/0 = rejected) · `resolution_note` String(2000) nullable · `provider_dispute_id` String(256) nullable · `created_at` · `resolved_at` nullable |
| `adjustments` | `id` UUID pk · `job_id` FK · `dispute_id` FK · `kind` enum refund/clawback/chargeback_loss/chargeback_fee · `amount` Numeric(12,2) **always positive — the kind carries the sign** (refund/loss/fee reduce net margin; clawback increases it) · `provider_ref` String(256) nullable · `created_at` |

New enums in `models.py`: `DisputeSource`, `DisputeStatus`, `AdjustmentKind`.

## Provider port extensions (fake + Stripe adapters)

- `refund(provider_payment_id, *, idempotency_key, amount: Decimal | None = None)`
  — `None` keeps full-refund behavior (the cancel path is untouched); a
  Decimal issues a partial refund in minor units.
- New `reverse_transfer(provider_transfer_id: str, *, amount: Decimal,
  idempotency_key: str) -> ReversalResult` (frozen dataclass:
  `provider_reversal_id: str`). Partial transfer reversal — the clawback
  mechanism. Full reversals were already proven against real Stripe in the
  adversarial gauntlet; partial is the same API with an `amount`.
- `PaymentEvent` gains optional `amount_minor: int | None`,
  `outcome: str | None`, `related_id: str | None`. The Stripe adapter maps
  `charge.dispute.created` → kind `chargeback_opened` and
  `charge.dispute.closed` → `chargeback_closed` (`outcome` won/lost;
  `related_id` = the PaymentIntent/charge id that locates our `Payment`;
  `object_id` = the provider dispute id). The fake provider passes the new
  fields through verbatim from unsigned JSON.
- Fake test seams: record reversal calls + their idempotency keys (mirroring
  the existing `transfer_keys` pattern) and partial-refund amounts + keys.
- **Partial-reversal webhook rule (final-review fix):** a clawback via
  `reverse_transfer` issues a PARTIAL Stripe transfer reversal. Stripe still
  fires `transfer.reversed` for it, but the event object's `reversed` field
  stays `False` — the transfer itself is not undone, and the payout is still
  paid. `StripeProvider.parse_webhook` downgrades `transfer.reversed` to kind
  `ignored` unless `reversed` is `True`; only a FULLY reversed transfer maps
  to `transfer_failed`. Mapping every `transfer.reversed` unconditionally to
  `transfer_failed` would flip a paid payout to FAILED on every dispute
  clawback, mail the seller a false "payout failed", and invite an admin
  retry (`POST /v1/admin/payouts/{id}/retry`) that double-pays them under a
  fresh idempotency key.

## Idempotency keys

Dispute refund: `refund:{job_id}:dispute` — deliberately distinct from the
cancel path's `refund:{job_id}` so a post-completion partial refund can never
replay a full cancel refund. Clawback: `reversal:{job_id}:dispute`. One
dispute per job makes these unique per operation.

## Resolution execution (the convergence pattern)

`POST /v1/admin/disputes/{id}/resolve {refund_amount, clawback_amount, note}`:

1. Guards: dispute exists (404); **resolve-guard rule (final-review fix):**
   status is not `resolved` (409) — `resolved` is the only terminal state.
   `chargeback_won`/`chargeback_lost` disputes remain arbitrable, because
   one-dispute-per-job means a lost chargeback would otherwise permanently
   block the platform from ever clawing back the at-fault seller (no second
   dispute can ever exist for that job). Amounts quantized and within bounds
   (422): refund ≤ job.buyer_price, clawback ≤ job.seller_payout. A clawback
   additionally requires the job's `Payout` to be `PAID` with a
   `provider_transfer_id` set (409 "no paid transfer to claw back" otherwise)
   — a `FAILED` payout (e.g. a fully reversed transfer) has no money the
   seller actually kept, so clawing back against it would book a lying
   `CLAWBACK` row (fake provider) or 502 forever (real Stripe).
2. **Pinned-amounts retry rule (final-review fix):** once every 4xx guard
   above has passed, the dispute's `refund_amount`/`clawback_amount` are set
   to the requested amounts and committed *before* either provider leg runs.
   That pin survives a subsequent 502's rollback (a provider leg may already
   have executed by the time it happens), so a later retry is checked
   against it: if the dispute already carries pinned amounts that differ
   from the new request, the retry gets 409 instead of silently diverging
   from what the provider already did. Real Stripe enforces the same
   convergence per idempotency key anyway; this just makes the failure
   legible instead of a mystery once the books and the provider might
   already disagree.
3. Provider legs, each only when its amount > 0: partial refund, then partial
   reversal — both idempotent by key. `PaymentError` on either → 502 with
   **nothing FURTHER recorded** (the pin from step 2 still stands); a retry
   with the SAME amounts replays the succeeded leg by key and completes the
   other. No partial-resolution state exists beyond the pin.
4. One transaction: the dispute row is re-locked (the pin's commit in step 2
   released the row lock taken in step 1) and set to `resolved` with amounts
   + note + `resolved_at`; `adjustments` rows appended (refund and/or
   clawback kinds, provider refs); notifications enqueued (below). The
   re-lock must be a `populate_existing` SELECT ... FOR UPDATE (or an
   explicit refresh) — a plain `get` with `with_for_update` takes the lock
   but, with `expire_on_commit=False`, hands back the unexpired identity-map
   object with stale attributes (verified empirically on Postgres). If
   the fresh state is already `resolved`, a concurrent duplicate resolve won
   the race during the lock-free legs window: the loser 409s — its provider
   legs were idempotent replays (money moved once), but booking again would
   double the ledger. A concurrent `chargeback_closed` that changed status
   mid-flight instead (e.g. to `chargeback_lost`) loses to arbitration by
   design: `resolved` overwrites it here, and the status-collision rule
   below stops any LATER chargeback event from undoing it.
5. **`Payment.status` is NOT touched.** A partial refund leaves the charge
   partly intact — the payment stays `SUCCEEDED` and the adjustment row is
   the record. `REFUNDED` remains reserved for the cancel path's full refund
   (and stays terminal for webhook events, per the auth-era fix).

## Chargeback flow (via the existing webhook endpoint)

- `chargeback_opened`: locate `Payment` by `related_id` → job. If the job has
  no dispute, create one (`source=provider`, `reason="provider chargeback"`,
  status `open`, `provider_dispute_id` set). If a buyer dispute already
  exists, annotate it with `provider_dispute_id` instead of duplicating.
  Notify admins. Unknown `related_id` → recorded (dedup ledger) and ignored,
  never a 500.
- `chargeback_closed` with `outcome=won`: status `chargeback_won`; no money
  moves (Stripe returns the withheld funds).
- `outcome=lost`: status `chargeback_lost` + `chargeback_fee` (always;
  `CHARGEBACK_FEE_USD` setting, default 15.00 — Stripe's fee isn't reliably
  present in the event payload; forks reconcile exact fees from balance
  transactions) + `chargeback_loss` (`amount_minor` from the event) —
  **only when that amount is > 0** (folded minor, final-review fix): a
  zero/absent `amount_minor` books no $0.00 loss row, since it isn't a real
  charge. Notify admins with the outcome.
- **Status collision rule:** `chargeback_closed` changes `status` unless the
  dispute is `resolved`. If an admin already resolved it, the status stays
  `resolved` — but the loss/fee adjustments are STILL appended and the admins
  still notified (a resolved dispute followed by a lost chargeback is a real
  double-loss; the ledger records both, the status field records the
  arbitration outcome). Otherwise the latest provider outcome wins: since the
  schema is one-dispute-per-job, a second chargeback on the same job
  re-annotates the existing row (`chargeback_opened`) and its
  `chargeback_closed` re-adjudicates the status (e.g. a prior
  `chargeback_lost` can flip to `chargeback_won`) — the ledger still records
  every outcome independently, only the status field reflects the latest.
- **Arbitration after a chargeback (final-review fix):** the reverse
  ordering — a chargeback closes first (`chargeback_won`/`chargeback_lost`),
  THEN an admin arbitrates — is also supported. `resolve_dispute`'s guard
  (resolve-guard rule, above) only rejects an already-`resolved` dispute, so
  a `chargeback_lost` dispute can still be resolved (e.g. to claw back the
  at-fault seller). The resulting `resolved` status is then protected by the
  status collision rule above against any further chargeback event.
- Rides the existing signature-verified, deduped `/v1/payments/webhook` —
  replayed events no-op.

## Endpoints and asymmetric views

| Endpoint | Behavior |
|---|---|
| `POST /v1/jobs/{job_id}/dispute` `{reason}` | buyer, own job, COMPLETED, within window, no existing dispute → `open`. 404 not-yours; 409 not-completed / window-elapsed / already-disputed |
| `GET /v1/jobs/{job_id}/dispute` | buyer's view |
| `GET /v1/seller/jobs/{job_id}/dispute` | seller's view (respondent; 404 unless the job is theirs) |
| `GET /v1/admin/disputes?status=` | paginated arbitration queue |
| `POST /v1/admin/disputes/{id}/resolve` | as above |

Views extend the information-asymmetry doctrine: `BuyerDisputeOut` carries
`refund_amount` and never the clawback; `SellerDisputeOut` carries
`clawback_amount` and never the refund; `AdminDisputeOut` carries everything.
`reason` and `status` are visible to all three (the seller must know what
they're accused of). No job view changes — dispute state lives behind the
dedicated GETs.

## Notifications (six new EventKinds through the existing outbox)

`DISPUTE_OPENED_SELLER` (reason, job) · `DISPUTE_OPENED_ADMIN` (fan-out) ·
`DISPUTE_RESOLVED_BUYER` (refund amount) · `DISPUTE_RESOLVED_SELLER`
(clawback amount) · `CHARGEBACK_OPENED_ADMIN` · `CHARGEBACK_CLOSED_ADMIN`
(outcome + amounts). Payloads role-safe at enqueue, as always. Renderer
registry completeness is already enforced by an existing test
(`test_every_kind_has_a_renderer`).

## Margin reporting

`MarginSummaryOut` gains `adjustments_net: Decimal` (clawbacks − refunds −
chargeback losses − chargeback fees) and `platform_margin_net: Decimal`
(= platform_margin + adjustments_net). Gross fields unchanged.

## Settings

`dispute_window_days: int = 7` · `chargeback_fee_usd: Decimal = Decimal("15.00")`.

## Error handling summary

Opening a dispute is a pure INSERT (no provider contact). Resolution follows
the 502-and-converge pattern with logging at each failure site (per the
payments doctrine: every money-failure path logs). Webhook branches never
500 on unknown ids or replays.

## Testing

Deterministic; **suite must be green on BOTH SQLite and Postgres before the
branch is done** (standing rule):

- Open-guards: not-yours 404, not-completed 409, window-elapsed 409
  (white-box `completed_at` aging), duplicate 409, happy 201.
- Resolve: full (refund=price, clawback=payout), partial (independent
  amounts), reject (0/0 — no provider calls, no adjustments) — fake provider
  records partial amounts + keys; adjustments rows appear; margin summary
  nets correctly (gross unchanged).
- Convergence: `fail_next_call` at the refund leg → 502, nothing recorded;
  retry succeeds; fake key-recording proves the refund leg replayed the SAME
  key. Same for the reversal leg.
- Key separation: dispute-refund key ≠ cancel-refund key.
- Chargebacks: opened → dispute row + admin mail; closed-lost → two
  adjustments + status; closed-won → no adjustments; duplicate event no-ops;
  unknown related_id ignored; annotate-not-duplicate when a buyer dispute
  already exists.
- Asymmetry: buyer dispute view/mail never contains the clawback amount;
  seller view/mail never contains the refund amount.
- Admin queue status filter; resolve of non-open dispute 409.

## Also updated

Demo act (dispute → partial resolution → net margin printed) · README
(disputes section + endpoint map) · CLAUDE.md invariants (`adjustments` is
append-only and `Transaction` rows are immutable — resolutions never edit
booked rows; dispute views are role-scoped like job views) · SECURITY.md
(dispute surface: buyer-only open, admin-only resolve, bounds validation at
the trust boundary) · ROADMAP (disputes + partial refunds → Done; remaining
T&S sub-phases listed in order) · `.env.example`.

## Constraints carried forward

uv · ruff + ruff format · pyright strict · zero new dependencies · SQLite
default / Postgres via `DATABASE_URL`, both green before done · ORM never
leaves the API layer · pricing/matching core untouched · identity from
principal only · Decimal money via `to_money` · notifications enqueue-only in
transactions · never gate on piped test output.
