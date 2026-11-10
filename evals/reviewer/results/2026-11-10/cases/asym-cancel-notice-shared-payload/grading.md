# Grading: asym-cancel-notice-shared-payload (bug)

Location: src/marketplace/services/jobs.py: _notify_cancelled

Invariant: Notification payloads are role-safe snapshots: seller payloads never carry buyer_price, buyer payloads never carry seller_payout.

Origin: Synthetic: no historical instance. The rule it breaks is the notifications design's role-safe payload rule (CLAUDE.md 'Notifications are enqueue-only'; asymmetry-reviewer item 5 names exactly this shape).

Rubric (fixed in the case file, sha256 0812261436fd):

CAUGHT: a Critical or Important finding at _notify_cancelled stating that the
shared payload puts buyer_price into the seller's JOB_CANCELLED_SELLER
notification and/or seller_payout into the buyer's REFUND_ISSUED_BUYER
notification (stored in the outbox row and visible to that party).
PARTIAL: objects to the shared dict as role-unsafe without naming which
amount reaches which party, or names it only at Minor severity.
MISSED: anything else (for example "renderers ignore extra keys, so fine").

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
