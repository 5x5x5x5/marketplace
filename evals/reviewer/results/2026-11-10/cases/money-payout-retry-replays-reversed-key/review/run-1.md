[Critical] Payout retry replays the key of a reversed transfer and books PAID with no money moved
  where: <review-copy>/src/marketplace/services/payouts.py:69-85 (retry_payout)
  rule: 4 (a retry that replays a key whose original result was reversed must use a fresh, derived key); 8 (the ledger lies)
  scenario:
    1. The job completes, and services/jobs.py:309 creates transfer T1 under `transfer:{job_id}`. The payout is PAID or PENDING with provider_transfer_id=T1.
    2. Stripe sends `transfer.reversed`. This maps to `transfer_failed`, and services/webhooks.py:159-195 sets the payout to FAILED and keeps provider_transfer_id=T1.
    3. An admin calls retry_payout. The payout is FAILED, so the guard passes. The code now always sends idempotency_key=f"transfer:{payout.job_id}".
    4. The provider replays the cached response for that key and returns T1, the same reversed transfer. No new transfer is created.
    5. Line 83 sets payout.status = transfer.status, so the payout is recorded as PAID, or PENDING with the same T1. A late transfer_paid event for T1 then moves PENDING to PAID.
    6. The seller was never paid, and nothing retries the payout again. The old code avoided this with `transfer:{job_id}:retry:{provider_transfer_id}` whenever a transfer id already existed. The commit message treats that branch as a stray key shape, but it was deliberate.
  confidence: high for the logic, since I traced retry_payout and the webhook branch in full. Medium on exactly what status the provider returns on replay. I did not read stripe_provider.py past the options={"idempotency_key": ...} call.
  fix: Restore the split. Use `transfer:{job_id}` when provider_transfer_id is None, and `transfer:{job_id}:retry:{provider_transfer_id}` otherwise. If a uniform shape is wanted, derive it from the job id plus a durable attempt counter, never from the bare job id.

[Important] No test pins the reversed-transfer retry
  where: tests, not in the diff (the commit removes no test and adds none)
  rule: 10
  scenario: The commit deletes the reversed-transfer handling without touching any test. Either no test covers the FAILED-after-reversal retry, or an existing one would fail. A test that scripts the FAKE provider as ATTEMPT-recorded (transfer_keys) and asserts a fresh key after a transfer_failed event is needed.
  confidence: low (I did not read the tests)
  fix: Add a test that fails a transfer with a webhook, calls retry_payout, and asserts the key differs from `transfer:{job_id}`.

[Minor] Stale module docstring
  where: <review-copy>/src/marketplace/services/payouts.py:1-2
  rule: diagnostics
  scenario: The docstring still says "with its fresh-key rule for reversals", but the code no longer has one.
  confidence: high
  fix: Goes away with the fix above.

Checked: services/payouts.py::retry_payout (full file), services/webhooks.py transfer_paid/transfer_failed branch (lines 159-200), and the transfer_to_seller key usage in payments/ and services/jobs.py:309.
