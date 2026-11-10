# Money-path audit — 2026-11

A review of every path that moves money or changes a job, offer, payment,
payout or dispute status, against five bug classes:

1. **Inverted lock order**: two paths lock the same two rows in opposite
   orders and deadlock on Postgres.
2. **Stale-snapshot dispatch**: a handler branches on an unlocked read, then
   locks without re-reading (finding F5 was one).
3. **4xx after a side effect**: the idempotency middleware stores and replays
   2xx and 4xx but never 5xx, so a 4xx raised after a provider call replays
   forever while money has moved.
4. **Retries that do not converge**: a retry after an outage, a 5xx or a
   crash between the provider call and the commit must reuse the same
   provider key and resume the same operation.
5. **Resurrection**: a late or replayed event flips a terminal state back.

Method: enumerate every provider call site and every place two or more of
{payment, job, offer, seller profile, payout, dispute} are locked or written
together (handlers, webhook branches, sweeps, admin paths, dispute
resolution), build the two tables below, then judge each path. Every
confirmed bug got a failing test first. Race tests run on Postgres only
(SQLite serializes writers), in `tests/test_money_path_audit.py`.

Two confirmed and fixed (A1, A2), six deferred with reasons (D1–D6), the
rest checked and found safe.

## Lock-order table

Order in which each path takes row locks (`FOR UPDATE`) or writes rows (an
`UPDATE` also takes the row lock). `[provider]` marks the network call.
After the fixes below.

| Path | Order |
|---|---|
| `accept_offer` | Offer → SellerProfile → Job → `[charge]` → insert Payment |
| `decline_offer` | Offer → Job → insert Offer (re-match) |
| `_sweep_expired_offers` | all expired Offers (`SKIP LOCKED`) → each Job (`populate_existing`) → insert Offer |
| `_sweep_stale_payments` | per stale job: Payment → Job (`populate_existing`) → `[void]` |
| `cancel_job`, `admin_cancel_job` | Payment (if one exists) → Job (`populate_existing`) → open Offers (`SKIP LOCKED`, A1) → Payment re-select → `[refund or void]` |
| `complete_job` | Job → `[transfer]` → insert Transaction, Payout → update SellerProfile, BuyerProfile counters |
| `retry_payout` | Payout → read SellerProfile → `[transfer]` |
| `resolve_dispute` | Dispute → read Job, Payment, Payout → commit (pin) → `[refund, reversal]` → Dispute (`populate_existing`) → insert Adjustments |
| webhook `payment_succeeded` / `payment_failed` | Payment → Job |
| webhook `transfer_paid` / `transfer_failed` | Payout → read Dispute (A2) |
| webhook `account_updated` | update SellerProfile |
| webhook `chargeback_opened` | read Payment → Dispute |
| webhook `chargeback_closed` | Dispute, or read Payment → Dispute |
| webhook retry job | due WebhookEvents (`SKIP LOCKED`) → per row as above, all held to one commit |
| `create_job` | Quote → insert Job, Offer |
| `onboard_payments` | read SellerProfile → `[create account]` |

Canonical orders: **Payment → Job** and **Offer → SellerProfile → Job**.
Before A1 the cancel paths wrote Offer rows after taking the Job lock, the
one inversion of the second order.

## Retry table

What a second attempt does after each failure point, per provider call site.

| Call site | Provider call | Key | Provider error | Failure after the call, before commit (or a crash) | 4xx reachable after the call? |
|---|---|---|---|---|---|
| `accept_offer` | `charge_buyer` | `charge:{job_id}` | 502, all rolled back, offer still open | 500 (not stored); the retry replays the same PaymentIntent. If the offer expired meanwhile the retry 410s and the job re-matches; the next accept replays the same key, because it is derived from the job | no |
| `complete_job` | `transfer_to_seller` | `transfer:{job_id}` | payout recorded FAILED, completion commits; admin retry replays the same key | 500; job still ACCEPTED, retry replays the transfer | no |
| `retry_payout` | `transfer_to_seller` | `transfer:{job_id}` with no prior transfer, else `transfer:{job_id}:retry:{old_transfer_id}` | 502 | 500; the row is unchanged, so the retry derives the same key and replays | no |
| `cancel_job`, `admin_cancel_job` | `refund` (full) | `refund:{job_id}` | 502 | 500; payment still SUCCEEDED, retry replays | no |
| same | `cancel_charge` | none; the Stripe adapter treats "already cancelled" as success | 502 | retry voids again, a no-op | no |
| `_sweep_stale_payments` | `cancel_charge` | as above | logged, job left for the next sweep | next sweep voids again | n/a |
| `resolve_dispute` | `refund` (partial), `reverse_transfer` | `refund:{job_id}:dispute`, `reversal:{job_id}:dispute` | 502; amounts were pinned and committed first, so the retry must reuse them and replays the leg that ran | dispute still OPEN with the pin; retry replays both legs and books once | the duplicate-resolve 409 (safe: the winner already booked); before A2, "no paid transfer to claw back" |
| `onboard_payments` | `create_seller_account` | `acct:{seller_id}` | 502 | 500; retry replays the same account | no |

## A1 — Cancel deadlocks against accept, decline and the offer sweep (inverted lock order)

- **Where:** `cancel_job` and `admin_cancel_job` (`api.py`), through
  `_expire_open_offers`.
- **Interleaving:** `accept_offer` locks the Offer, then the SellerProfile,
  and is about to lock the Job. A cancel of the same job locks the Payment
  (none yet), then the Job, then `_expire_open_offers` sets the open offer to
  EXPIRED; the autoflush `UPDATE offers` blocks on accept's offer lock.
  Accept asks for the Job lock and blocks on the cancel. Postgres detects
  the cycle after `deadlock_timeout` and kills one side: a 500 for the
  buyer's or admin's cancel, or for the seller's accept. `decline_offer`
  (Offer → Job) and `_sweep_expired_offers` (all expired Offers, then each
  Job) meet a cancel the same way. The sweep is the widest window: it holds
  every expired offer's lock while it re-matches job after job, and it runs
  on ordinary GETs.
- **Severity:** Medium. No money moves on the losing side (the deadlock
  rolls it back and a 500 is never stored for replay), but a buyer's cancel
  or a seller's accept fails at random under contention, on a path that runs
  on every read.
- **Fix:** `_expire_open_offers` claims open offers
  `FOR UPDATE SKIP LOCKED`. An offer held by accept, decline or the sweep is
  left to its holder, which finds the job no longer PENDING once the cancel
  commits: accept 409s ("job is no longer open"), decline and the sweep do
  not re-match. An offer skipped this way stays OFFERED until its TTL, when
  the sweep expires it; accepting it meanwhile 409s.
- **Test:** `test_cancel_vs_accept_never_deadlocks[buyer|admin]` (Postgres).
  It widens the window by slowing `repo.active_job_count`, which accept
  calls while holding the offer and seller locks. Before the fix the cancel
  died with `DeadlockDetected` (500); after, cancel 200 and accept 409 with
  no Payment row. Stable over repeated runs.

## A2 — A full dispute clawback reads as a failed transfer and invites a second payout

- **Where:** `_apply_payment_event`'s `transfer_failed` branch (`api.py`),
  fed by `StripeProvider.parse_webhook`.
- **Lead confirmed.** `resolve_dispute` allows
  `clawback_amount == seller_payout`. Stripe sends `transfer.reversed` for
  every reversal; the adapter maps it to `transfer_failed` when the
  transfer's `reversed` field is true, which is exactly the full-reversal
  case (pinned by
  `test_stripe_provider::test_full_transfer_reversal_maps_to_transfer_failed`).
- **Failure sequence:** the admin resolves with a full clawback → the
  reversal leg reverses the whole transfer → `transfer.reversed`
  (`reversed: true`) arrives as `transfer_failed` → the payout goes PAID →
  FAILED and admins get `PAYOUT_FAILED_ADMIN` → the admin follows the page
  and retries the payout → `retry_payout` sees a transfer id and uses a fresh
  `...:retry:{old_transfer_id}` key, so it pays the seller the full payout
  again, on top of the clawback the platform just took back.
- **Second consequence (class 3):** the refund leg succeeds and the
  reversal leg errors client-side (a timeout) although the provider ran it,
  so the resolve 502s. The full-reversal event lands before the retry. The
  payout is FAILED by then, so the retry's guard answers 409 "no paid
  transfer to claw back", stored for replay. The dispute stays OPEN, the
  buyer has been refunded, and the ledger has no REFUND row.
- **Severity:** Critical (double payout reachable by following the admin
  page), Important for the stuck dispute.
- **Fix:** the `transfer_failed` branch reads the job's dispute
  `clawback_amount`; when it covers the whole payout, the payout keeps its
  status and nobody is paged (an info log records it). The pin is committed
  before the reversal leg runs, so it is visible whether the event arrives
  first, late from the retry job, or twice, and it is a permanent row rather
  than the reapable webhook ledger. A full reversal with only a partial
  clawback pinned is someone else's doing and still marks the payout FAILED.
- **Tests:** `test_full_clawback_reversal_event_does_not_fail_the_payout`
  (payout stays PAID, no page, payout retry 409s, no second transfer),
  `test_full_clawback_reversal_event_twice_and_late` (deferred to the retry
  job, then replayed),
  `test_partial_clawback_does_not_mask_a_real_transfer_failure`,
  `test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry`.

## Deferred

- **D1 — Cancel locks Payment after Job when an accept creates the payment
  mid-cancel.** The cancel paths lock the Payment before the Job, but if
  none exists yet and a concurrent accept inserts one while the cancel waits
  for the Job lock, the cancel locks that Payment later, in
  `_release_payment`, after the Job. A `payment_succeeded` webhook taking
  Payment → Job in that gap would deadlock with it. The webhook would have to
  arrive within the few milliseconds between the accept's commit and the
  cancel's re-select, for a PaymentIntent created inside that same accept.
  The outcome is self-healing: the detector kills one side, a killed webhook
  goes to the outbox and its late apply is safe (REFUNDED is terminal, a
  voided charge is never revived), and a killed cancel 500s and can be
  retried. Fix shape if wanted: in the cancel paths, take the second
  Payment lock with `NOWAIT` and answer 503 on a lock error.
- **D2 — Provider idempotency keys expire.** Stripe keeps a key for about
  24 hours. Every convergence argument in the retry table assumes the retry
  lands inside that window. A payout retry under the original
  `transfer:{job_id}` key days after a timed-out transfer that actually ran
  would pay again; a full refund retry would be rejected by Stripe (charge
  already refunded) and 502 forever; a late account-creation retry would
  make a second connected account. Fix shape: look the object up by metadata
  (`transfer_group`, PaymentIntent) before a retry outside the window.
  Design work, not a local fix.
- **D3 — Lost update on completion counters.** `complete_job` reads
  SellerProfile and BuyerProfile without a lock and writes
  `completed_jobs += 1` at commit, across the transfer round trip. Two
  completions for the same seller (different jobs) can both write N+1. Not
  cash, but a pricing input (first-job discount, seller tiers). Fix shape:
  an SQL-side increment (`UPDATE ... SET completed_jobs = completed_jobs + 1`).
- **D4 — `resolve_dispute` re-runs its existence guards on a retry.** After
  A2, the platform's own clawback can no longer flip the payout between
  attempts. A full reversal made outside the platform (say from the provider
  dashboard) between a 502 and its retry would still turn the retry into a
  replayable 409 after the refund leg ran. Fix shape: when amounts are
  already pinned, skip the guards that an earlier attempt passed. Left alone
  because it changes the documented retry contract and needs an operator
  mistake to reach.
- **D5 — A2's marker can hide an external full reversal.** If a full
  clawback is pinned but its reversal leg never runs (the admin never
  retries the 502) and someone reverses the transfer outside the platform,
  the payout stays PAID and the dispute retry 502s on the reversed transfer.
  Money is not lost, but it needs a manual fix. Accepted as the cost of
  keeping the marker a permanent row.
- **D6 — Stale-payment sweep iterates without an order.** It holds every
  stale job's Payment and Job locks until one commit, in the order of an
  unordered `SELECT`. Two concurrent sweeps with the same plan get the same
  order, so the second simply waits; not reproduced. Fix shape:
  `.order_by(Job.id)`.

Lead from earlier workers, out of scope: `accept_offer`, `complete_job`,
both cancel paths, `retry_payout` and the stale-payment sweep hold row
locks and a pooled connection across the provider round trip. Restructuring
that is a design change. Each was checked for classes 1–5 as it stands (the
tables above); A1 is the only finding among them.

## Checked and safe

- **Stale snapshots (class 2).** Every lock that follows an unlocked load of
  the same row re-reads it: both cancel paths' Job re-get and the
  stale-payment sweep (F5), the offer sweep's Job re-get, and
  `resolve_dispute`'s post-leg Dispute re-lock. Every other locked get is the
  first touch of that row in its session (accept's Offer, SellerProfile and
  Job; decline's Job; complete's Job; `retry_payout`'s Payout; the webhook's
  Payment and Job). No ORM relationships exist, so nothing loads a row
  behind the handler's back. In the webhook retry job, one session applies
  many rows, but every lock is held until the batch commits and savepoint
  rollback expires what it touched. Proven by `test_cancel_vs_complete_race`
  and `test_cancel_vs_webhook_race`.
- **Payment → Job order.** The webhook, the stale-payment sweep and both
  cancel paths agree; `complete_job` never locks the Payment, so a
  redelivered `payment_succeeded` racing a completion just waits.
- **Accept vs completion on one seller.** `complete_job` writes the
  SellerProfile after its Job lock (Job → SellerProfile), the reverse of
  accept. A cycle would need accept to wait on the job being completed, which
  needs an open offer on an ACCEPTED job to the same seller; one offer per
  (job, seller) rules it out.
- **Webhook before accept commits.** A pending charge needs the buyer to
  confirm with the `client_secret`, which only reaches them in accept's
  response, after the commit; a charge that succeeds synchronously is already
  SUCCEEDED in the row. So an early event that finds no Payment loses
  nothing.
- **4xx after a side effect (class 3).** `accept_offer`, `complete_job`, the
  cancel paths, `retry_payout`, the sweep and `onboard_payments` have no 4xx
  reachable after their provider call; failures there are 500 or 502 and are
  never stored. `resolve_dispute` hoists its 4xx guards above the legs; its
  post-leg 409 fires only when a concurrent resolve already booked the
  ledger (`test_concurrent_duplicate_resolve_cannot_double_book`). Its
  remaining case was A2.
- **Convergence (class 4).** See the retry table. Proven by
  `test_provider_outage_rolls_accept_back`,
  `test_admin_retries_failed_payout`,
  `test_reversed_transfer_retry_forces_a_new_transfer`,
  `test_partial_failure_retry_replays_succeeded_leg_by_key`,
  `test_resolve_pins_amounts_across_a_502_retry`,
  `test_sweep_void_failure_leaves_job_for_next_sweep` and
  `test_onboard_provider_outage_is_502_then_recovers`.
- **Resurrection (class 5).** The payment lattice: REFUNDED is terminal, a
  failure never undoes a success, and a success after a void never revives a
  dead job (`test_stale_success_never_resurrects_a_refund`,
  `test_late_success_never_resurrects_a_voided_cancel`, with the legitimate
  decline-then-retry recovery kept by
  `test_decline_then_retry_on_same_pi_recovers`). A late `transfer_paid`
  never turns FAILED back to PAID; a late `transfer_failed` for a replaced
  transfer matches no payout
  (`test_transfer_failed_late_for_a_replaced_transfer_is_a_noop`). Chargeback
  events never undo RESOLVED. Cancelled and expired jobs have no path back
  to an active status.

Other observation, not in the five classes: `onboard_payments` does not
catch a `PaymentError` from `provider.onboarding_link`, so that failure is a
bare 500 rather than the 502 every other provider failure gets. Nothing is
lost (the account id rolls back and the retry replays `acct:{seller_id}`).
