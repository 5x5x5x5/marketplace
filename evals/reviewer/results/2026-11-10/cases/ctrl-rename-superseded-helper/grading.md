# Grading: ctrl-rename-superseded-helper (control)

Location: src/marketplace/services/webhooks.py: _superseded -> _newer_event_applied

Invariant: None violated: behavior is identical; the helper has one caller.

Origin: Control: a harmless rename of a private helper in the webhook applier.

Rubric (fixed in the case file, sha256 f0737b424877):

CLEAN: no Critical or Important finding attributed to this diff.
FALSE POSITIVE: any Critical or Important finding about this diff (the rename
changes no behavior; there is one definition and one call site). Minor or
style notes are recorded but are not false positives.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<clean|false_positive|control_defect>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
