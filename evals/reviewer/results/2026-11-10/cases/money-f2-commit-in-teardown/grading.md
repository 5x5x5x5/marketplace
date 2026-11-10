# Grading: money-f2-commit-in-teardown (bug)

Location: src/marketplace/db.py: get_session and CommitRoute.get_route_handler

Invariant: Commits happen in db.CommitRoute, before the response is sent, never in get_session teardown.

Origin: F2 (docs/tryout-findings-2026-07-16.md). Fixed by the commit-before-response branch (docs/superpowers/plans/2026-07-16-commit-before-response.md).

Rubric (fixed in the case file, sha256 eaaceb8f1293):

CAUGHT: a Critical or Important finding at db.get_session / CommitRoute
stating that the commit now runs in dependency teardown, which FastAPI runs
after the response has been sent: a client can receive a 2xx and act on
(chain a call against) state that is not committed yet, and a failing commit
leaves a 2xx for work that never persisted.
PARTIAL: objects to committing in teardown or to removing CommitRoute's
commit without the after-the-response consequence, or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
