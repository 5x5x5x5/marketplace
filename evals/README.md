# Evals

## `reviewer/`: which layer of defense catches which real bug

This repository was built by coding agents and reviewed by narrow reviewer
agents, and its history holds a set of real defects that a review, a test or
a live try-out eventually caught: a seller receipt that leaked the buyer's
price and the margin (F1), a commit that landed after the response was sent
(F2), a cancel racing a completion into a refund of a completed job through
a stale re-read (F5), a cancel deadlocking against an accept (A1), a full
dispute clawback read back as a failed transfer (A2), a late payment success
reviving a voided payment, and more. Each is documented with its mechanism
and its fix in `docs/tryout-findings-2026-07-16.md`,
`docs/money-path-audit-2026-11.md`, `SECURITY.md` and `git log`.

This eval re-seeds those defects into the current code and asks, per bug:

| Layer | What runs |
|---|---|
| Tests | the full suite on SQLite, and on Postgres (several of these bugs only bite under true concurrency) |
| Architecture tests | `tests/test_architecture.py` on its own (excluded from the test columns) |
| Semgrep | the repo's own rules, `.semgrep/rules/` over `src` and `scripts` |
| Reviewer agent | the narrow reviewer in `.claude/agents/` that owns the bug's class, reading the diff |

The rows worth reading are the bugs only the reviewer catches and the bugs
nothing catches. Control cases (correct changes of the same size in the
same areas) measure false positives: a layer that flags everything is not
useful.

### What this does not measure

- **It is a regression suite for a review process, not a benchmark.** About
  a dozen hand-seeded cases in one repository say nothing about reviewers in
  general, and catch rates over n=16 move a lot with one case.
- **The seeded bugs are ones this process already knows about.** Every
  historical case was fixed with a regression test, an architecture check or
  a line in a reviewer prompt that names it. That flatters every layer: the
  interesting question is which bugs slip past *despite* that.
- **A single run of a non-deterministic reviewer is an anecdote.** Run each
  packet several times (`run-1.md`, `run-2.md`, ...) before quoting a rate;
  the table shows counts per case.
- **Diffs are realistic, which sometimes helps the reviewer.** Where a real
  contributor removing a guard would also delete its explanatory comment, the
  case does too, and the deleted comment shows in the diff. Cases mark this
  (`removes_explanatory_comment`); read those catches with that in mind.
- **Mechanical results are deterministic only up to the race tests.** A
  Postgres race test can catch a mutant on one run and miss it on the next.
  The runner re-runs every failing test once and lists the ones that did not
  fail again.
- **Autoescape is not covered yet.** The only way to bypass it is in
  `pages.py` or the templates, which were being rewritten when the cases were
  written; add a case once that settles.

### Cases

Each `reviewer/cases/<id>.toml` holds one mutation and its metadata:

- `id`, `kind` (`bug` or `control`), `class`, `origin` (finding id or commit
  subject; `synthetic = true` when the class has no historical instance)
- `invariant` it breaks and `location` (file and function) of the defect
- `reviewer` to run, and `reviewer_fit` (whether that reviewer's prompt
  names this class at all)
- `commit_message`: innocent, the kind a hurried contributor writes
- `rubric`: what a review must say to count as **caught**, **partial** or
  **missed** (controls: **clean** or **false positive**). The rubric names
  the location and the failure mode, not "this looks risky".
- `[[edits]]`: exact replacements, applied in order; each `old` must occur
  exactly once in its file as it stands after the earlier edits.

Rules for writing one: nothing in the mutated code or the message may name
the bug; the mutant must pass ruff, ruff format and pyright (a submitted
change would); never tune a case to make a layer look better or worse; if a
layer surprises you, that is the result.

`tests/test_reviewer_eval_cases.py` (in the normal suite, under a second)
fails when a case no longer applies to the tree. When you move code, re-anchor
the case on the moved code with the same defect.

### Add a case for every new bug

This is the habit that keeps the suite worth having. When a review, a test,
a try-out or production finds a bug:

1. Fix it as usual, with its regression test.
2. Add `reviewer/cases/<class>-<short-name>.toml` that re-introduces the bug
   into the fixed code as the smallest plausible diff, with the origin
   (finding id or fix commit subject), invariant, location, reviewer, an
   innocent commit message and a rubric written now, before any review of it.
3. `python3 evals/reviewer/run.py check`, then run the mechanical layers for
   that case. If its own regression test does not catch the re-seeded bug,
   the test is weaker than it looks: fix the test, and say so.

### Running it

```bash
# validate every case against the working tree (no writes)
python3 evals/reviewer/run.py check

# mechanical layers for every case; packets and the table land in
# evals/reviewer/results/<date>/. Postgres databases <name>_s0.. are
# created when --jobs > 1.
python3 evals/reviewer/run.py mechanical --jobs 3 \
    --pg-url postgresql+psycopg://marketplace:marketplace@localhost:5432/t_eval
python3 evals/reviewer/run.py mechanical --cases money-f5-admin-cancel-stale-relock
```

Each run exports HEAD (not the working tree) without `evals/` and the guard
test into a fresh git repository with no history, so nothing the reviewer or
the tests can read holds the answer. It runs an unmodified baseline first;
a layer catches a case only with failures or findings the baseline does not
have. Semgrep is fetched with `uvx`; if it cannot be, the column says
"not run".

### The reviewer step

The runner never calls a reviewer. For each case,
`results/<date>/cases/<id>/packet/` holds `prompt.md` (the exact text to give
the reviewer, with the diff), `diff.patch`, `commit-message.txt` and
`HARNESS.md` (which reviewer to run and how; not for the reviewer's eyes).

1. `python3 evals/reviewer/run.py checkout <id> /tmp/review-<code>` builds the
   review copy: the mutation committed on branch `change` over `main`.
2. Run the named reviewer agent with that directory as its working directory
   and `prompt.md` as its task. Mention neither the evaluation nor the case.
3. Save its raw final output, unedited, as
   `results/<date>/cases/<id>/review/run-1.md` (then `run-2.md`, ...).
4. Grade each output against `results/<date>/cases/<id>/grading.md` (the
   rubric, copied from the case file) and write `review/verdict.json`:

   ```json
   {"grader": "who graded",
    "runs": [{"output": "run-1.md", "verdict": "caught",
              "rationale": "quotes the finding that decided it"}]}
   ```

   Bugs: `caught`, `partial`, `missed`. Controls: `clean`, `false_positive`,
   or `control_defect` when the grader confirms the "correct" change is in
   fact wrong. Caught needs the location and the failure mode at Critical or
   Important severity; the right finding at Minor is partial.
5. `python3 evals/reviewer/run.py assemble --results evals/reviewer/results/<date>`
   rebuilds `results.md` and `results.json`. It refuses verdicts without
   their raw output and flags any case file changed since the mechanical run.

Grade blind to the other layers' results if you can, and keep every raw
output, so a reader can disagree with a verdict.
