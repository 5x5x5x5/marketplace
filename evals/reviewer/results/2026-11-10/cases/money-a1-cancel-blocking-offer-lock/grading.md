# Grading: money-a1-cancel-blocking-offer-lock (bug)

Location: src/marketplace/services/offers.py: expire_open_offers (called by cancel_job and admin_cancel_job)

Invariant: Canonical lock order Offer -> SellerProfile -> Job; a path already holding the Job lock claims Offer rows FOR UPDATE SKIP LOCKED, never a blocking lock.

Origin: A1 (docs/money-path-audit-2026-11.md). Fixed in 7287d30 'Skip offers locked by accept/decline/sweep when a cancel expires them'.

Rubric (fixed in the case file, sha256 9963ff494394):

CAUGHT: a Critical or Important finding at expire_open_offers / the cancel
paths stating that cancel holds the Job lock and now blocks on an Offer lock,
while accept_offer (or decline, or the offer-expiry sweep) holds that Offer
and waits for the Job: an inverted lock order that deadlocks on Postgres
(one side killed, 500).
PARTIAL: flags a lock-order or deadlock risk without naming the opposing path
or the Job-then-Offer inversion, or names it only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
