# Grading: ctrl-stale-sweep-ordered (control)

Location: src/marketplace/services/maintenance.py: _sweep_stale_payments

Invariant: None violated: Payment -> Job lock order per job is unchanged; a deterministic iteration order only makes concurrent sweeps take locks in the same order.

Origin: Control: the fix shape the money-path audit proposed for deferred item D6 (stale-payment sweep iterates without an order).

Rubric (fixed in the case file, sha256 7cdfd984c268):

CLEAN: no Critical or Important finding attributed to this diff.
FALSE POSITIVE: any Critical or Important finding about this diff (ordering
by primary key changes only iteration order). Pre-existing issues in the
function that the diff does not touch are not attributed to the diff. Minor
notes (for example "add a test") are not false positives.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<clean|false_positive|control_defect>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
