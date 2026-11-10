[Critical] Admin cancel re-lock no longer refreshes the Job, so a cancel racing complete can refund the buyer and pay the seller
  where: <review-copy>/src/marketplace/services/jobs.py:362 (admin_cancel_job)
  rule: item 1 (stale re-lock). CLAUDE.md: "A lock taken after an unlocked read uses `populate_existing` and re-checks status."
  scenario:
    1. Job J is ACCEPTED. The admin cancel handler runs the unlocked `session.get(Job, job_id)` at line 357. That caches J in the identity map with status=ACCEPTED, and `expire_on_commit=False` keeps it cached.
    2. The handler locks the Payment at line 361. Meanwhile a concurrent `complete_job` holds the Job lock. It commits J=COMPLETED, books the Transaction and creates the Payout, then releases.
    3. Line 362 now calls `session.get(Job, job_id, with_for_update=True)` without `populate_existing`. The identity map already holds J, so SQLAlchemy returns the cached object with its stale attributes. The SELECT FOR UPDATE does take the lock, but the cached attributes are not refreshed from the returned row.
    4. The guard at line 365 reads the stale status ACCEPTED and passes. `_release_payment` then refunds or voids the buyer's payment, and the job is set to CANCELLED over the committed COMPLETED row.
    5. Final state: the seller was paid and the margin booked by complete, the buyer was refunded, and the job reads CANCELLED. This is the original F5 bug, documented in the removed comment and in docs/tryout-findings-2026-07-16.md.
    The commit message calls the option redundant. It is not. The lock does not refresh an already-cached object. The unlocked get at line 357 is exactly the pattern that makes the option necessary.
  confidence: high (traced lines 357-376 and every other `with_for_update` Job re-get; jobs.py:91, offers.py:116 and maintenance.py:56 all keep `populate_existing=True`)
  fix: Restore `populate_existing=True` at line 362 and the explanatory comment.

[Important] No test appears to guard the F5 relock on the admin cancel path
  where: <review-copy>/src/marketplace/services/jobs.py:357-366 (admin_cancel_job)
  rule: item 10 (missing proof). The diff touches no tests and drops the option anyway. That suggests no race test fails on the removal, but I did not search tests/ for such a test.
  scenario: As in the Critical finding. A Postgres race test of admin cancel against complete, asserting no refund once the job is COMPLETED, would have caught this change.
  confidence: medium
  fix: Add a Postgres race test of admin cancel against complete, or confirm that one exists and why it did not fail.

Checked: admin_cancel_job in full (jobs.py:354-376); lock order Payment then Job (correct, unchanged); a grep of every `populate_existing` and `with_for_update` Job re-get (jobs.py:91, offers.py:116, maintenance.py:56 all retain it); CLAUDE.md concurrency rule. I did not open tests/.
