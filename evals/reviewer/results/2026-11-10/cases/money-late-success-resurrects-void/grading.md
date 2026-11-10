# Grading: money-late-success-resurrects-void (bug)

Location: src/marketplace/services/webhooks.py: _apply_payment_event, payment_succeeded branch

Invariant: Every _apply_payment_event branch is safe to apply late: a success after a void never resurrects a dead job's payment.

Origin: 3c5d1af 'Never resurrect a voided payment: late success no-ops on dead jobs' (SECURITY.md later fixes: 'Late payment success').

Rubric (fixed in the case file, sha256 8f25ede4a314):

CAUGHT: a Critical or Important finding at the payment_succeeded branch
stating that a late or replayed success now flips a payment voided by a
cancel or the stale-payment sweep (FAILED, job CANCELLED/EXPIRED) back to
SUCCEEDED, leaving the forbidden pair dead job + succeeded charge: the buyer
is charged for a job that will never run and no refund is booked.
PARTIAL: says FAILED -> SUCCEEDED should be guarded on job state without the
consequence, or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
