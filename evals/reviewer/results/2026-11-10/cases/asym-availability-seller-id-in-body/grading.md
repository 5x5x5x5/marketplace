# Grading: asym-availability-seller-id-in-body (bug)

Location: src/marketplace/models.py: AvailabilityRequest; src/marketplace/services/profiles.py: post_availability

Invariant: Identity comes from the authenticated principal, never a request body. Never add a buyer_id/seller_id body field.

Origin: SECURITY.md safe-to-pilot sweep C3/C4 (seller identity taken from caller-supplied input instead of the token); the class behind the 'identity comes from the principal' rule.

Rubric (fixed in the case file, sha256 fc864fe92ded):

CAUGHT: a Critical or Important finding stating that POST
/v1/seller/availability now takes seller_id from the request body and uses it
over the token's identity, so any seller can post availability as (and steer
matching toward or away from) another seller: impersonation.
PARTIAL: says identity must come from the principal but does not tie it to
this field and route, or reports it only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
