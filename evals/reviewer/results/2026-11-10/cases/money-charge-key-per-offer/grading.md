# Grading: money-charge-key-per-offer (bug)

Location: src/marketplace/services/jobs.py: accept_offer

Invariant: Outbound idempotency keys are derived from the job id (charge:{job_id}) so any retry of the charge, whoever accepts, replays the original PaymentIntent.

Origin: Synthetic: no historical instance. Derived from the money-path audit's retry table (accept_offer row: after a crash between charge and commit, the next accept, possibly by another seller after re-match, must replay the same key 'because it is derived from the job').

Rubric (fixed in the case file, sha256 92e43bd01296):

CAUGHT: a Critical or Important finding at accept_offer stating that the
charge key is no longer stable per job: if an accept charges and then fails
or crashes before commit, the job can be re-offered (expiry/re-match) and the
next accept charges again under a new key, so the buyer is charged twice and
the first PaymentIntent is orphaned with no Payment row to refund.
PARTIAL: says keys must be derived from the job without the double-charge
path, or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
