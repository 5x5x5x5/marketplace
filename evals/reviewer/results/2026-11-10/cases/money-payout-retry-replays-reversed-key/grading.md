# Grading: money-payout-retry-replays-reversed-key (bug)

Location: src/marketplace/services/payouts.py: retry_payout

Invariant: A retry that would replay a key whose original result was reversed must use a fresh, derived key (money-path-reviewer item 4).

Origin: b41be67 'Apply final-review fix wave: ... reversed-transfer retry keys ...' (retry_payout must not replay the key of a reversed transfer).

Rubric (fixed in the case file, sha256 47f8b164f72d):

CAUGHT: a Critical or Important finding at retry_payout stating that when the
original transfer was created and later reversed/failed, replaying
transfer:{job_id} returns that same reversed transfer from the provider, so
the payout is recorded PAID while no money reaches the seller.
PARTIAL: says reversed transfers need a fresh key without the
recorded-PAID-but-unpaid consequence, or only at Minor severity.
MISSED: anything else, including agreeing the key should always be replayed.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
