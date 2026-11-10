[Important] Retention sweep deletes FAILED webhook rows that are still owed a retry
  where: <review-copy>/src/marketplace/services/maintenance.py:106 (_sweep_retention)
  rule: CLAUDE.md "PENDING outbox rows are never reaped" (a FAILED webhook_events row is exempt because it is still owed a retry), and item 7 (webhooks).
  scenario:
    1. A payment or dispute event (a charge success, a chargeback, or a transfer.reversed) fails in its handler. It is persisted FAILED with next_attempt_at, and the endpoint answers 200 "deferred". The provider will not redeliver it.
    2. The retry job stalls. Examples are the maintenance loop being disabled on every replica, a handler that fails for longer than retention_webhooks_days, or a manual /v1/admin/webhooks/{id}/retry that never happens.
    3. received_at is older than the window, so the new time-only DELETE removes the FAILED row. run_webhook_retry_once selects FAILED rows, so it can no longer find it.
    4. The effect is lost for good, with no DEAD_LETTER row and no admin page. Money state diverges from the provider: a payment stays PENDING, or a dispute or clawback is never applied.
    5. The commit message's "the provider will not redeliver" argument covers APPLIED rows only. For FAILED rows the ledger row is the only record that the work is owed.
    6. The retry job claims due rows with SKIP LOCKED and applies them under a savepoint. A concurrent sweep DELETE on a claimed row blocks on the lock and then removes it after the retry's commit. That is harmless if the retry succeeds, but it is the same reaping race on a retry that fails.
  confidence: high (I read _sweep_retention and the retry and dedup paths in services/webhooks.py. I did not trace the retry loop line by line.)
  fix: Restore the WebhookEvent.status != WebhookEventStatus.FAILED filter and the import. Keep the docstring note.

[Important] Test and docs that encode the FAILED exemption were not updated
  where: <review-copy>/tests/test_webhook_outbox.py:466 (test_retention_spares_failed_rows_and_reaps_terminal_ones)
  rule: item 10 (missing proof). This is also a CLAUDE.md invariant change made without updating CLAUDE.md or the test.
  scenario: The commit only touches maintenance.py. The existing test asserts that FAILED rows older than the window survive. After this change it should fail, which means the change either breaks the gate or removes the invariant without anyone deciding to. The CLAUDE.md bullet still says FAILED is exempt.
  confidence: medium (I did not run the test. I inferred its content from its name and the docstring at line 4.)
  fix: Revert the code change. If the invariant is really being changed, change CLAUDE.md and the test deliberately and add a dead-letter step first.

Checked: services/maintenance.py::_sweep_retention in full; the WebhookEvent status handling, retry claim, _record_webhook_failure and dedup paths in services/webhooks.py (by grep and partial read); test references to retention.
