[Critical] Revert removes the full-clawback guard, so a dispute clawback marks the payout FAILED and an admin retry pays the seller again
  where: <review-copy>/src/marketplace/services/webhooks.py:164-165 (_apply_payment_event, transfer_failed branch)
  rule: 7 (provider event mapped by name instead of by what happened; the "transfer_failed ... pinned dispute clawback" rule in CLAUDE.md) and 5 (double effect)
  scenario:
    1. A job completes. Its Payout is PAID with provider_transfer_id tr, amount 14.00.
    2. An admin resolves the dispute with clawback_amount=14.00. services/disputes.py pins the amount, commits, then calls reverse_transfer (key reversal:{job}:dispute).
    3. Stripe reports the full reversal as transfer.reversed with reversed=true. stripe_provider.py:212-215 maps that to transfer_failed.
    4. The branch now sets payout.status = FAILED unconditionally and enqueues PAYOUT_FAILED_ADMIN.
    5. An admin sees a FAILED payout and calls POST /v1/admin/payouts/{id}/retry. payouts.retry_payout (line 64) accepts FAILED and creates a new transfer under a fresh key (transfer:{job}:retry:{tr}), because the old one was reversed.
    6. The seller is paid the full payout a second time, on top of the clawback. The buyer's refund and the platform's clawback no longer net out, and the ledger disagrees with the cash.
  confidence: high. I traced the adapter mapping, the removed query, and the retry path. The commit deletes the only guard against this. The removed code was also what made the branch replay-safe, because the clawback pin is a permanent row.
  fix: Restore the Dispute.clawback_amount >= payout.amount check before setting FAILED. If the commit message's goal is to mirror the provider, do it by a different mechanism, not by dropping the guard.

[Important] Three regression tests that this commit breaks were not updated
  where: <review-copy>/tests/test_money_path_audit.py:123 (test_full_clawback_reversal_event_does_not_fail_the_payout), :152 (test_full_clawback_reversal_event_twice_and_late), and a third test at about :195 (test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry)
  rule: 10 (missing proof). I did not run the suite, so the failures are by reading.
  scenario: The first two tests assert that the payout stays PAID and that no PAYOUT_FAILED_ADMIN notification is sent after a full-clawback transfer_failed event. Both fail with this change. The third test, which I only skimmed, covers a full reversal landing mid-retry and likely fails too. The commit touches no tests, so it was either not run or the failures were ignored.
  confidence: medium
  fix: Restore the guard (see the finding above). Do not delete the tests.

Checked: git show HEAD; the full _apply_payment_event transfer_paid/transfer_failed branch in services/webhooks.py; the transfer.reversed mapping in payments/stripe_provider.py (lines 195-224); services/payouts.py retry_payout (FAILED gate and fresh-key rule); the clawback/reversal call site in services/disputes.py; the A2 tests in tests/test_money_path_audit.py. I did not run the tests.
