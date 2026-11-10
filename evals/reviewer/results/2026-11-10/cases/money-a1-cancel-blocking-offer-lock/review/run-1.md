[Critical] Blocking offer lock under the Job lock reintroduces an ABBA deadlock with accept, decline and the sweep
  where: <review-copy>/src/marketplace/services/offers.py:87-96 (expire_open_offers); callers jobs.py:96 (cancel_job) and jobs.py:371 (admin_cancel_job)
  rule: item 3 (lock order). CLAUDE.md says canonical orders are Payment -> Job and Offer -> SellerProfile -> Job. A path that already holds the Job lock must claim Offer rows `FOR UPDATE SKIP LOCKED`, never a blocking lock.
  scenario:
    1. A buyer cancels a PENDING job. cancel_job locks Payment, then Job (jobs.py:87-91), then calls expire_open_offers.
    2. A seller accepts the same offer at the same moment. accept (jobs.py:181-198) has already locked the Offer row and is now waiting on the Job lock (jobs.py:198).
    3. The cancel's `SELECT ... FOR UPDATE` on Offer now waits for accept's Offer lock, and accept waits for the cancel's Job lock. Postgres detects the deadlock and aborts one of them with a 500.
    4. Decline (jobs.py:258-265) and the sweep (offers.py:113) take Offer then Job in the same way, so they deadlock against a cancel too.
    5. The deleted comment described this hazard. The other half of the commit message's concern is already covered: the SKIP LOCKED holder finds the job no longer PENDING after the cancel commits, so accept 409s and decline and the sweep do not re-match. The "stays OFFERED" window the commit wants to close is therefore not reachable as a money or visibility bug beyond the TTL.
  confidence: high (I read both callers and the Offer-first paths. I did not run a Postgres test.)
  fix: Revert to `.with_for_update(skip_locked=True)`. To hide a leftover OFFERED offer from sellers, filter the seller offer list by `Job.status == PENDING`. Alternatively, have accept and decline lock the Job before the Offer, but that breaks the canonical order.

[Important] No Postgres race test for cancel racing accept, decline or the sweep
  where: tests (rg for `skip_locked` and `expire_open` in tests returned nothing)
  rule: item 10 (missing proof)
  scenario: The change flips a lock mode on a path with a documented deadlock. Nothing exercises cancel concurrent with accept on Postgres, so the deadlock above would reach production unnoticed.
  confidence: medium
  fix: Add a Postgres race test with two sessions: one holds the Offer lock and waits on the Job, while cancel holds the Job and expires offers. Assert that neither deadlocks and that no offer is left OFFERED on a CANCELLED job.

[Minor] Stale CLAUDE.md and module docstring
  where: <review-copy>/CLAUDE.md:69 and <review-copy>/docs/money-path-audit-2026-11.md:91
  rule: documentation of a rule the diff now contradicts
  scenario: CLAUDE.md still mandates SKIP LOCKED for the cancel paths, so the code and the stated invariant disagree. Revert the code and this resolves.
  confidence: high
  fix: Revert the code change (see the first finding).

Checked: offers.expire_open_offers, sweep_expired_offers, jobs.cancel_job, jobs.admin_cancel_job, and the lock acquisition order in the accept (jobs.py:181-198) and decline (jobs.py:258-273) paths.
