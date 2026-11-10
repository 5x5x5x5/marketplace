# Grading: money-f5-admin-cancel-stale-relock (bug)

Location: src/marketplace/services/jobs.py: admin_cancel_job

Invariant: A lock taken after an unlocked read uses populate_existing and re-checks status (CLAUDE.md concurrency rule; money-path-reviewer item 1).

Origin: F5 (docs/tryout-findings-2026-07-16.md). Fixed in 12309c6 'Fix seller receipt leak (F1) and cancel-vs-complete double-pay (F5)'.

Rubric (fixed in the case file, sha256 aefcef809d49):

CAUGHT: a Critical or Important finding at admin_cancel_job stating that the
locked get returns the identity-map object cached by the earlier unlocked
session.get (expire_on_commit=False), so the status guard reads pre-lock
state; a cancel racing a seller completion can then cancel/refund a job that
is already COMPLETED (buyer refunded while the seller is paid).
PARTIAL: says populate_existing is needed after an earlier load without the
concrete race outcome, or gives the outcome only at Minor severity.
MISSED: anything else, including agreeing the option was redundant.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
