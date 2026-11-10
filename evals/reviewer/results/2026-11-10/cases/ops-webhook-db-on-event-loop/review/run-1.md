[Important] Sync webhook processor now runs on the event loop
  where: <review-copy>/src/marketplace/routers/payments.py:32 (payments_webhook)
  rule: 7 (DB work in services.webhooks.process_webhook via asyncio.to_thread); CLAUDE.md "Sync DB work stays off the event loop"
  scenario: The route is `async def`. `process_webhook` (services/webhooks.py:410) is a plain sync function. It opens a SessionLocal and runs the dedup select, flush, `_apply_payment_event`, commit, and `_persist_failed_webhook` on failure. The commit removed `asyncio.to_thread`, so all of that now executes on the coroutine.
  1. Event A arrives and flushes its dedup row.
  2. Event B, a concurrent first delivery with the same event id, flushes and blocks on the unique-constraint lock until A commits. This wait is documented in the function's own docstring ("the loser waits for the winner").
  3. If both run inline, B holds the loop while it waits on A's transaction. A may itself be a handler that needs the loop, or a handler stalled on a row lock (Payment/Job).
  4. Every other in-flight request stalls, and on Postgres this can wedge the whole worker.
  A slow apply or DB lock wait also blocks all requests for its full duration. The commit message calls the thread hop "more than the work", but that cost is not the criterion here. The CLAUDE.md rule is that nothing calls a session inline in an `async def`.
  confidence: high (I read the router and the function body; I did not trace `_apply_payment_event`'s provider calls)
  fix: Restore `return await asyncio.to_thread(_process_webhook, event)` (or `run_in_threadpool`) and the `import asyncio`.

[Minor] Module docstring now contradicts the code
  where: <review-copy>/src/marketplace/routers/payments.py:1-3
  rule: 10 (diagnostics)
  scenario: The docstring says "dedup and apply run on a worker thread ... never on the event loop", which this change makes false. Reverting the main finding resolves it.
  confidence: high
  fix: Revert the change, or update the docstring if the offload is intentionally removed (it should not be).

Missing proof: no test guards the offload. `tests/test_architecture.py` or a semgrep rule should assert that the webhook route does not call a sync session function inline.

Checked: routers/payments.py payments_webhook in full; services/webhooks.py process_webhook (dedup, flush, IntegrityError path, apply and commit, failure path up to `_persist_failed_webhook`). Dedup and replay semantics are unchanged by this diff.
