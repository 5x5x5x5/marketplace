---
name: money-path
description: Checklist for any change that moves or records money or changes a job/offer/payment/payout/dispute status in this marketplace - charges, transfers, refunds, clawbacks, cancels, completion, webhooks, sweeps, capacity. Covers row-lock order and stale re-reads, deterministic provider idempotency keys, commit-before-response and mid-handler commits, flush-before-return, webhook replay safety, ledger immutability, and role-scoped views. Use before writing such a change and again before calling it done.
---

# Money-path change checklist

Every rule here is a bug this repo actually shipped or nearly shipped. Most
were invisible to the in-process suite and surfaced only in a final review, a
Postgres race test, or a live try-out. Read the whole list; then go through it
against your diff before you call the task done, and ask for the
`money-path-reviewer` subagent on the branch.

## Locking and concurrency

1. **Lock with the database, never a process lock**:
   `session.get(Model, id, with_for_update=True)` for every status transition,
   quote consumption, and capacity check. There may be several workers.
2. **A lock that follows an earlier unlocked load must re-read**:
   `session.get(..., with_for_update=True, populate_existing=True)` (or a
   `select(...).with_for_update().execution_options(populate_existing=True)`).
   With `expire_on_commit=False`, a plain locked `get` takes the row lock but
   returns the cached object with pre-race attributes. Finding F5: admin cancel
   did an unlocked existence check, then a locked re-get that returned a stale
   ACCEPTED status, so a cancel racing a complete refunded the buyer while the
   payout stayed paid. Double-pay, hit on the first live attempt.
3. **Re-check status after every lock-free window.** A mid-handler commit or a
   provider call releases or outlives locks; re-lock with `populate_existing`
   and re-validate before booking. Dispute resolution double-booked the ledger
   when a duplicate resolve won during the provider legs, until it re-checked.
4. **Canonical lock order: `Payment` before `Job`.** The webhook applier, the
   stale-payment sweep, and both cancel paths all lock in that order. Taking
   them the other way round deadlocked cancel against a webhook on Postgres.
5. **A new race gets a Postgres test** (`@pytest.mark.skipif(not IS_POSTGRES, ...)`,
   threads, a barrier, several attempts), modelled on
   `test_cancel_vs_complete_race`. SQLite serializes writers, so the race test
   must cross into a forbidden state to prove anything: the original
   cancel-vs-webhook race test passed 20/20 because both statuses it crossed
   were allowed.

## Provider calls

6. **Only through `payments/port.py`** (the edit guard blocks `import stripe`
   elsewhere). New capability = port method + Stripe adapter + fake adapter
   with recorded calls/keys for tests.
7. **Idempotency keys are derived, never random**: `charge:{job_id}`,
   `transfer:{job_id}`, `refund:{job_id}`, `acct:{seller_id}`. A retry must
   replay the original result instead of charging twice.
8. **Distinct operations get distinct keys.** Dispute refunds use
   `refund:{job_id}:dispute`, never the cancel path's `refund:{job_id}`, so a
   partial refund can't replay a full one. A payout retry after the transfer
   was reversed uses `transfer:{job_id}:retry:{old_transfer_id}`: replaying
   the original key returns the reversed transfer and would record PAID with
   no money moved.
9. **Every provider failure logs and answers 502**, never a bare 500 and
   never swallowed.

## Commit ordering

10. **`CommitRoute` commits after the handler, before the response is sent.**
    Never commit in `get_session` teardown: teardown runs after the client has
    the response (finding F2, the read-your-writes race on chained calls).
    A new router sets `route_class=CommitRoute`; `_assert_commit_routes`
    fails at import if not.
11. **`CommitRoute` commits only 2xx.** If state must survive an error
    response, commit mid-handler with a comment naming why. Precedents: the
    dispute amounts are pinned and committed before the provider legs so a 502
    retry converges on the same amounts; a dead pinned quote is deleted and
    committed before the 410.
12. **Flush before returning an ORM object.** The response model is
    serialized from it before the commit, so an unflushed object can carry a
    missing server-generated id or timestamp.

## Webhooks

13. **Record the event id in `WebhookEvent` before applying it**; a replay
    no-ops. Test the replay by asserting the effect happened once, not by the
    response string (an early dedup test could not tell a short-circuit from a
    re-run that happened to be harmless).
14. **Terminal states stay terminal.** A late `payment_failed` never undoes a
    success; nothing resurrects a `REFUNDED` payment.
15. **Map provider events by what happened, not their name.** A partial
    transfer reversal (a dispute clawback) still fires `transfer.reversed`
    with `reversed=False`; mapping it to `transfer_failed` would flip a paid
    payout to FAILED and invite an admin retry that pays the seller twice.
16. **Unknown ids are recorded and ignored, never a 500.** DB work stays in
    `_process_webhook`, which runs in `asyncio.to_thread`.

## Ledger and money values

17. **`Decimal` everywhere, quantized with `models.to_money`**, serialized as
    strings. The pricing pipeline stays `float`; quantize at the boundary.
18. **The margin floor is net of fees** (`matching.required_spread`) at both
    enforcement sites. `Payment.fee_estimate` is stamped once at charge time
    and never recomputed.
19. **`Transaction` rows are immutable; `adjustments` is append-only.** A
    resolution appends `refund`/`clawback`/`chargeback_*` rows (positive
    amounts, the kind carries the sign). A partial refund never touches
    `Payment.status`; `REFUNDED` means the cancel path's full refund.
20. **`AWAITING_PAYMENT` holds a capacity slot.**

## Who sees what

21. **Role-scoped views and notification payloads.** A seller response or
    seller notification never carries `buyer_price`, `margin`, or buyer
    identity; a buyer's never carries `seller_payout`. Finding F1: the seller's
    completion receipt returned the admin `TransactionOut` (buyer price and
    margin) for months, because no asymmetry test covered that endpoint. A
    new money response gets its own asymmetry assertion. See the
    `endpoint-or-page` skill.

## Proving it

- Failing test first. For ordering bugs, TestClient is structurally blind
  (it awaits the whole app cycle); use the `live_server` fixture and
  `slow_commits` from `tests/conftest.py`.
- `gate` skill with `--pg`. Then the `tryout` skill against a running server
  if the change alters the lifecycle.
