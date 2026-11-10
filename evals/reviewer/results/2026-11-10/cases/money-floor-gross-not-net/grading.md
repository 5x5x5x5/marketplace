# Grading: money-floor-gross-not-net (bug)

Location: src/marketplace/matching.py: _priced (used by every matching strategy)

Invariant: The margin floor check is net-of-fees: compare against matching.required_spread(buyer_price, margin_floor, fees), never the gross spread.

Origin: 28b5c33 'Make the margin floor fee-aware at both enforcement sites' (fee-aware margin phase; match-time test added in dd92407).

Rubric (fixed in the case file, sha256 4c2b66a0723e):

CAUGHT: a Critical or Important finding at matching._priced stating that the
inlined check compares the spread against effective_floor only, dropping the
estimated provider fee that required_spread adds, so matching accepts
payouts whose margin is below the floor once fees are paid (net-negative or
under-floor jobs), and diverges from the quote and pinned job-time checks.
PARTIAL: notices the check differs from passes_floor/required_spread without
naming the dropped fee, or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
