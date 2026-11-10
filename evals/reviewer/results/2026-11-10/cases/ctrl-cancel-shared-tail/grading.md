# Grading: ctrl-cancel-shared-tail (control)

Location: src/marketplace/services/jobs.py: cancel_job, admin_cancel_job, new _cancel_locked

Invariant: None violated: lock order Payment -> Job -> Offers (SKIP LOCKED) and populate_existing are unchanged; the extracted helper runs under the same locks.

Origin: Control: a behavior-preserving refactor of both cancel paths (same lock order, same locked re-reads, same provider calls).

Rubric (fixed in the case file, sha256 9419be1a8d50):

CLEAN: no Critical or Important finding attributed to this diff.
FALSE POSITIVE: any Critical or Important finding about this diff that the
grader cannot confirm by reading the code (the refactor preserves lock order,
the populate_existing re-reads, the provider calls and their error mapping,
and the order notify -> status change). Minor or style notes are recorded but
are not false positives. If the grader confirms a real defect, record
"control_defect" and report it.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<clean|false_positive|control_defect>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
