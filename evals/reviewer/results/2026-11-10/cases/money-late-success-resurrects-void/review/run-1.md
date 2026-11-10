[Critical] Late `payment_succeeded` now marks a voided payment SUCCEEDED while the job stays dead, so the buyer is never refunded
  where: src/marketplace/services/webhooks.py:130-134 (`_apply_payment_event`, `payment_succeeded` branch)
  rule: 7 (a late event must not undo a terminal state, and the comment on lines 121-124 still promises "a success after a void never resurrects a dead job"). Also 8, because the ledger and cash disagree.
  scenario:
    1. A buyer cancels an AWAITING_PAYMENT job, or the stale-payment sweep releases it. `_release_payment` (`services/jobs.py:141-145`) calls `cancel_charge` and sets Payment to FAILED. The job becomes CANCELLED or EXPIRED.
    2. A `payment_succeeded` event for that PI arrives late, from a race with the void or a retry. This is the case the deleted guard handled, and `test_payment_succeeded_late_after_buyer_cancel_never_resurrects` (tests/test_webhook_replay.py:108-124) covers it.
    3. The status is no longer REFUNDED, so the branch continues. It now sets `payment.status = SUCCEEDED` unconditionally.
    4. The job is not AWAITING_PAYMENT, so it stays CANCELLED.
    5. End state: Payment SUCCEEDED on a dead job. The cancel and sweep paths only refund when a payment is SUCCEEDED at the moment they run, and both have already run. Nothing will ever issue the refund, so the buyer is charged for a cancelled job.
    6. If the provider really did capture, the platform holds the money with no booking. If it didn't, the Payment row now claims a capture that never happened.
    7. The commit's justification, "decline-then-retry relies on FAILED -> SUCCEEDED", only concerns a job still in AWAITING_PAYMENT. The old guard allowed that case, so removing it was not needed for it.
    8. The existing test asserting `(CANCELLED, FAILED)` at line 124 should now fail. The commit changes no tests.
  confidence: high
  fix: Restore the guard. If the payment is FAILED and the job is missing or not AWAITING_PAYMENT, log a warning and return. Better, also refund or page an admin when a real capture lands on a dead job.

Checked: `_apply_payment_event` payment_succeeded/payment_failed branch, including lock order (Payment before Job, fine); `services/jobs.py::_release_payment`; the late-event tests in tests/test_webhook_replay.py (lines 95-165).
