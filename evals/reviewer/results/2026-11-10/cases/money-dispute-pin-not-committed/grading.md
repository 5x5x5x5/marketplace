# Grading: money-dispute-pin-not-committed (bug)

Location: src/marketplace/services/disputes.py: resolve_dispute

Invariant: A handler that must persist state ahead of a possible error response commits mid-handler: resolve_dispute pins refund/clawback and commits before calling the provider.

Origin: 32a6f31 'Close disputes final-review findings: partial-reversal mapping, pinned resolve retries, ...' and 51252f1; the pin-and-commit precedent in CLAUDE.md. Also the precondition A2's fix relies on (b63c870).

Rubric (fixed in the case file, sha256 f9ee7b3c0a11):

CAUGHT: a Critical or Important finding at resolve_dispute stating that
without the commit the pinned refund/clawback amounts roll back when a
provider leg fails (502, which CommitRoute never commits), so a retry can
use different amounts after a leg already moved money; and/or that the
clawback pin is no longer visible to the transfer_failed webhook while the
reversal runs, so a full clawback reads as a failed payout.
PARTIAL: says the mid-handler commit is deliberate/required without the
retry-divergence or webhook-visibility consequence, or only at Minor severity.
MISSED: anything else, including endorsing the single-transaction argument.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
