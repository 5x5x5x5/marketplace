# Grading: ops-webhook-db-on-event-loop (bug)

Location: src/marketplace/routers/payments.py: payments_webhook

Invariant: Sync DB work stays off the event loop: process_webhook runs via asyncio.to_thread; nothing calls a session inline in an async def.

Origin: df4b9c0 'Add retention sweeps, move webhook DB work off the event loop'.

Rubric (fixed in the case file, sha256 de410f756ea5):

CAUGHT: a Critical or Important finding at payments_webhook stating that the
async handler now runs synchronous session work (process_webhook's dedup,
row locks, commit) on the event loop, so a slow or lock-blocked webhook stalls
every other in-flight request in the process.
PARTIAL: says it should stay on a thread without the stall consequence, or
only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
