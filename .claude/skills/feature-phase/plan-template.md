# <Phase name> Implementation Plan

**Goal:** one sentence.

**Architecture:** one paragraph: the modules touched and how the pieces fit.

**Spec:** `docs/superpowers/specs/YYYY-MM-DD-<phase>-design.md` (approved). Branch: `<phase>`.

## Global Constraints

- Gate after every task, bare exit codes, never piped:
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
  (or `.claude/skills/gate/gate.sh`). Pyright strict.
- Failing test first; run it; quote the red output in the task report.
- Zero new dependencies unless listed here.
- Stage exact paths; commit only the task's listed files.
- Phase invariants: <restate the ones from the spec>.
- Before the branch is done: gate green on SQLite and Postgres (`gate.sh --pg`).

---

### Task 1: <name>

**Files:**
- Create: `...`
- Modify: `...` (anchor: function or line ~N)
- Test: `tests/test_<x>.py`

**Interfaces:**
- Consumes: <names from earlier tasks or existing code>
- Produces (later tasks rely on): <exact names and signatures>

- [ ] **Step 1: Write the failing test** (full code)
- [ ] **Step 2: Run it, expect FAIL** — `uv run pytest tests/test_<x>.py -q`
- [ ] **Step 3: Implement** (sketch; adapt to the file as it is)
- [ ] **Step 4: Gate**
- [ ] **Step 5: Commit** — `git add <paths>`; message: imperative subject, why in the body

---

### Task N: Demo, docs, both-backend verification

`scripts/demo.py` act, `README.md`, `SECURITY.md` update section, `ROADMAP.md`,
`CLAUDE.md` invariants, `.env.example`; `gate.sh --pg`.

## Self-review

- Spec coverage: each spec section -> task.
- Name consistency across tasks.
- Judgment calls made while planning.
- Placeholders: none.

## Final-review amendments

(Appended after the whole-branch review: each finding, the fix, the test.)
