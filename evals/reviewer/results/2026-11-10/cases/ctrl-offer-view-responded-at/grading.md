# Grading: ctrl-offer-view-responded-at (control)

Location: src/marketplace/models.py: SellerOfferView

Invariant: None violated: responded_at is the seller's own action timestamp on the seller-side view.

Origin: Control: a legitimate new field on the seller's own offer view (the seller's own response time; no counterparty data).

Rubric (fixed in the case file, sha256 b08f7c9995b1):

CLEAN: no Critical or Important finding attributed to this diff.
FALSE POSITIVE: any Critical or Important finding claiming this field leaks
counterparty data or breaks a rule. A Minor note asking for a test of the
new field is reasonable and is not a false positive.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<clean|false_positive|control_defect>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
