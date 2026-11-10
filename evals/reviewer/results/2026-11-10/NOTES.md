# Notes on the 2026-11-10 run

Read these before quoting the table in `results.md`.

## How the reviewer column was produced

- One review per case, 20 in all, each by the reviewer subagent named in
  the case (`.claude/agents/`), all on the same model. The model is not
  named in this repository.
- Each reviewer was given a scratch copy of the repository with the
  mutation committed on top (no `evals/` directory, no history) and the
  packet's `prompt.md` as its task. The task was passed as a file path
  plus one sentence saying where the repository was, not pasted inline.
- `review/run-1.md` is the reviewer's final report as handed back. The only
  edit is that the absolute path of the scratch copy is replaced with
  `<review-copy>`.
- Verdicts were assigned by one grader (the coordinating agent) against the
  rubric in each case file, which was committed before any review ran. The
  grader knew the seeded defect. Every rationale quotes the finding that
  decided it, so a reader can check.

## Why 16 of 16 overstates the reviewers

- **The bugs are known to the repository.** Fourteen of the sixteen come
  from this repo's history, and their rules are written in `CLAUDE.md`, in
  the reviewer checklists and in the findings documents, all of which the
  reviewer can read in the scratch copy. Several reviews cite the finding
  document for the very bug they were shown. This measures whether a
  reviewer applies rules the project already wrote down, not whether it
  finds new classes of bug. The money-path audit that found two real bugs
  is the better evidence of the second thing.
- **Some diffs delete the comment that explains the bug.** A realistic
  contributor would delete it, so the cases do too (they are marked
  `removes_explanatory_comment`), but the deleted comment is visible in the
  diff and makes the catch easier.
- **One run.** Reviewers are not deterministic. A single pass per case is
  an anecdote; the runner accepts `run-2.md`, `run-3.md` and so on, and a
  result worth quoting needs them.
- **One grading call was a judgment.** `ops-pagination-in-python` went to
  the spec-compliance reviewer, whose output format has categories, not
  severities. It was graded caught on substance; a strict reading of the
  rubric's "Critical or Important" would make it partial.
- **One reviewer read outside its copy.** The same reviewer cited the
  `CLAUDE.md` of the main checkout, not the scratch copy's. The rule it
  applied is identical in both, but the isolation was not perfect.

## What the mechanical columns say, which does not depend on any model

- Two cases pass every mechanical layer: a notification payload shared
  between buyer and seller, and a charge key derived from the offer instead
  of the job. Both are synthetic. Neither class has a test or a static
  check today.
- The regression tests for the stale re-lock (F5) and the inverted lock
  order (A1) only fail on Postgres. A SQLite-only CI run would pass both
  bugs. CI here runs both engines for that reason.
- With the dispute pin's commit removed, one Postgres test hangs instead of
  failing, because the wait runs through application code that Postgres
  cannot see. The suite has no per-test timeout, so CI would report a
  killed job, not a named test.
- The architecture tests police seller-facing fields but did not flag a
  `buyer_id` added to a seller view; a behavioral test caught it.
- None of the four control changes tripped any layer, including the
  reviewer.

## Follow-ups this run suggests

- A test that asserts role-safe payloads on the stored notification rows
  for every kind, not only the kinds that have one today.
- `buyer_id` in the architecture test's denylist for seller views.
- A per-test timeout in the suite.
- Repeat the reviewer runs, and add a case whenever a real bug is found.
