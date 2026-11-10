# Grading: asym-seller-review-buyer-id (bug)

Location: src/marketplace/models.py: SellerReviewOut (response of POST /v1/seller/jobs/{job_id}/review)

Invariant: Seller-facing views never carry buyer identity (asymmetry doctrine; .claude/agents/asymmetry-reviewer.md item 2).

Origin: 95a459a 'Fix wave: withhold buyer identity from sellers, guard PI-less chargebacks' (SellerReviewOut was the first seller-facing view to leak buyer_id).

Rubric (fixed in the case file, sha256 9f81e3bf66ad):

CAUGHT: a Critical or Important finding on SellerReviewOut (or the seller
review route that returns it) stating that sellers now receive the buyer's id
(buyer identity), which seller views must never carry.
PARTIAL: notes the new buyer_id field as questionable without saying it
breaks the seller-side identity rule, or says so only at Minor severity.
MISSED: anything else, including accepting the ReviewOut symmetry argument.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
