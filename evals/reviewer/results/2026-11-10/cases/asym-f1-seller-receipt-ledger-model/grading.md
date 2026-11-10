# Grading: asym-f1-seller-receipt-ledger-model (bug)

Location: src/marketplace/routers/seller.py: complete_job (POST /v1/seller/jobs/{job_id}/complete)

Invariant: Information asymmetry is enforced by the model layer: seller endpoints return seller views; seller payloads never carry buyer_price or margin (CLAUDE.md non-negotiables).

Origin: F1 (docs/tryout-findings-2026-07-16.md; SECURITY.md findings history). Fixed in 12309c6 'Fix seller receipt leak (F1) and cancel-vs-complete double-pay (F5)'.

Rubric (fixed in the case file, sha256 7bc475377773):

CAUGHT: a Critical or Important finding at the seller completion route
(routers/seller.py complete_job, POST /v1/seller/jobs/{id}/complete) stating
that the response now serializes TransactionOut, so the seller receives the
buyer's price and/or the platform margin.
PARTIAL: flags the model swap on that route as the wrong view family or an
asymmetry risk without naming buyer_price or margin as what leaks; or names
the leak correctly but only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
