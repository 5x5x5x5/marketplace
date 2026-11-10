# Grading: money-a2-full-clawback-reads-as-failure (bug)

Location: src/marketplace/services/webhooks.py: _apply_payment_event, transfer_failed branch

Invariant: transfer_failed for a transfer that a pinned dispute clawback covers in full is the platform's own reversal: the payout stays PAID and nobody is paged (CLAUDE.md webhook branch rule).

Origin: A2 (docs/money-path-audit-2026-11.md). Fixed in b63c870 'Keep a payout PAID when a full dispute clawback reverses its transfer'.

Rubric (fixed in the case file, sha256 c3014e43e024):

CAUGHT: a Critical or Important finding at the transfer_failed branch stating
that a full dispute clawback reverses the whole transfer, the provider reports
that as a failed/reversed transfer, and the payout now goes FAILED and pages
admins; an admin following the page retries the payout (fresh key) and pays
the seller again on top of the clawback.
PARTIAL: notes that dispute reversals and real failures are now
indistinguishable without the double-payout consequence, or gives it only at
Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
