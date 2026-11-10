# Grading: ops-retention-reaps-failed-webhooks (bug)

Location: src/marketplace/services/maintenance.py: _sweep_retention

Invariant: The retention sweep never reaps a FAILED webhook_events row (still owed a retry) or a PENDING notification.

Origin: df4b9c0 'Add retention sweeps, move webhook DB work off the event loop' (PENDING outbox never reaped) and f8f54dc 'Merge webhook outbox: persisted failures, retry, dead-letter, replay-safe handlers' (FAILED webhook rows exempt).

Rubric (fixed in the case file, sha256 f88a8b5455c7):

CAUGHT: a Critical or Important finding at _sweep_retention stating that the
delete now also removes FAILED webhook_events rows, which are the retry
outbox: a payment event still waiting for (or backing off between) retries is
deleted and never applied (lost payment/payout/chargeback state change), and
its dedup marker is gone.
PARTIAL: notes the status filter was dropped without saying FAILED rows are
pending retries, or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
