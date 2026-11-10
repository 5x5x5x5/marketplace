[Critical] Pinned dispute amounts no longer survive a provider 502, so a retry can book different amounts than the provider moved
  where: <review-copy>/src/marketplace/services/disputes.py:180-185 (resolve_dispute)
  rule: item 6 (state that must survive an error response is committed only by CommitRoute, which commits 2xx only); item 4 and item 8 (ledger lies). CLAUDE.md names this exact mid-handler commit as a required precedent.
  scenario:
    1. Admin POSTs resolve with refund=10 and clawback=5.
    2. The pin is only flushed. The refund leg succeeds at the provider under key `refund:{job}:dispute` for 10.
    3. The clawback leg fails with PaymentError, so the service raises ProviderUnavailableError (502).
    4. CommitRoute commits 2xx only, and get_session's close() rolls back. The pin is gone, but the provider has already refunded 10.
    5. The admin retries with refund=3. The "prior attempt pinned" 409 guard sees `dispute.refund_amount` as None and passes.
    6. The same key is replayed with a different amount. Real Stripe rejects this (a 502 loop). The fake provider ignores the amount and replays the original refund, so the ledger books a REFUND Adjustment of 3 while the provider moved 10.
  confidence: high (I read the full function and db.py. I did not run it. tests/test_disputes.py:623 asserts "the pin survived the rollback" and should now fail.)
  fix: Restore the mid-handler `session.commit()` after the pin, with a comment naming the mechanism. Keep the post-leg `populate_existing` re-lock, or find another way to keep the pin durable across a 502.

[Minor] Stale comments and docstring after the change
  where: disputes.py:3-5 (module docstring), disputes.py:~215 (comment "The pin's commit above released the row lock"); db.py:8 (comment)
  rule: item 6 (a mid-handler commit needs a comment naming why)
  scenario: Both comments still say the pin commits mid-call and releases the lock. Neither is true after this change, so they will mislead the next reader. Dropping the commit does keep the dispute lock held across the provider legs. The communicated rationale ("atomic single transaction") is the very thing that breaks retry convergence above. The re-lock with `populate_existing` is now redundant but harmless.
  confidence: high
  fix: Update the comments if the commit is restored. Otherwise drop the claims.

Checked: resolve_dispute in full (lock, guards, pin, provider legs, re-lock, ledger booking), the keys `refund:{job}:dispute` and `reversal:{job}:dispute`, the fake provider's refund and key handling, db.py CommitRoute and get_session commit and rollback semantics, and the test reference at tests/test_disputes.py:623.
