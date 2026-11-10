---
name: feature-phase
description: Run a new feature phase in this repo the way every existing phase was built - design spec, task-by-task implementation plan, test-first build with the gate after each task, adversarial whole-branch review by narrow reviewer subagents, a fix wave, a live try-out, docs, and a merge commit. Use when starting any feature bigger than a single-file fix, when asked to "plan", "spec", or "build a phase", or when resuming a half-built phase.
---

# Spec -> plan -> build -> review -> fix wave -> try-out -> merge

Twelve phases were built this way (`docs/superpowers/specs/`,
`docs/superpowers/plans/`; the directory name is historical, from the plugin
first used to drive the loop, which this skill does not need). The git
history shows the rhythm: a spec commit, a plan commit, one commit per task,
a "final review" or "fix wave" commit, docs, then a `Merge <phase>: ...`
commit. Follow it; each stage exists because skipping it once cost something.

## 1. Spec (`docs/superpowers/specs/YYYY-MM-DD-<phase>-design.md`)

Use `spec-template.md` beside this file. A spec settles **decisions**, not
code: eligibility rules, state transitions, the data model (and so the
migration), provider calls and their idempotency keys, which view family
sees which field, notifications, error semantics, settings, non-goals, and
the "constraints carried forward" from `CLAUDE.md`. Decisions the maintainer
must confirm are listed as such and confirmed before planning. The disputes
spec's numbered "final-review fix" rules show what a spec looks like after
review taught it something; amend the spec in place when that happens.

## 2. Plan (`docs/superpowers/plans/YYYY-MM-DD-<phase>.md`)

Use `plan-template.md`. A plan is executable by an agent with no other
context:

- **Global Constraints** at the top: gate command, bare exit codes, zero new
  dependencies unless the spec says so, stage exact paths (never
  `git add -A`), the phase's invariants restated.
- **Tasks** small enough for one commit each. Every task lists **Files**
  (create/modify/test, with line anchors that may drift), **Interfaces**
  (what it consumes from earlier tasks and produces for later ones, with
  exact names), and **Steps**: the failing test written out in full, the
  command that shows it red, the implementation sketch, the gate, and the
  commit message.
- A **self-review** section: spec coverage task by task, name consistency
  across tasks, judgment calls made, no placeholders.
- Migration, docs (`README.md`, `SECURITY.md`, `ROADMAP.md`, `CLAUDE.md`
  invariants, `.env.example`), and the demo act each belong to a task.

## 3. Build, one task at a time

Hand each task to the `task-implementer` subagent (or do it yourself the
same way): read the task and Global Constraints, write the failing test,
**run it and keep the red output**, implement, run the gate, commit only the
listed files. Then run `spec-compliance-reviewer` on that task's commit
before starting the next one: drift is cheap to fix one task in and
expensive five tasks in. Do not start task N+1 on a red gate.

## 4. Adversarial whole-branch review

When every task is in, review the whole branch diff (`git diff main...HEAD`)
with the narrow reviewers, in parallel:

- `spec-compliance-reviewer` against the spec and plan,
- `money-path-reviewer` if anything touches money, status, locks, webhooks,
- `asymmetry-reviewer` if anything adds or changes a response, page, or
  notification payload.

Narrow reviewers find what a general "review this" misses: the Stripe
partial-reversal double-pay, the stale re-lock, the cached 403 replay, the
buyer-id leak to sellers, and the cookie-blind idempotency principal were all
found here, after every task had passed its own review.

## 5. Fix wave

Fix every Critical and Important finding in one or a few commits named
"Fix wave: ..." or "... (final review)", each with a test that fails without
the fix. Then re-review the fixes themselves: a fix that adds a mid-handler
commit or a new lock is a new money-path change (the dispute pin fix opened a
lock-free window that the verification round caught). Append a
"Final-review amendments" section to the plan listing what changed and why,
rather than rewriting the task bodies.

## 6. Try-out against a live server

Run the `tryout` skill. The in-process suite cannot see commit ordering or
real clients: findings F1, F2, and F5 all surfaced here after green suites
and clean reviews. Record findings in the findings doc format; fix or
explicitly defer each.

## 7. Done means

- Gate green on SQLite **and** Postgres (`gate` skill with `--pg`), with
  counts reported; demo passes.
- Every spec decision implemented or listed as a deliberate deviation.
- Review findings closed or written down as accepted, with the reason.
- Docs updated in the phase, not after: `CLAUDE.md` gains any new
  invariant the phase introduced (that file is the contract the next agent
  reads), `SECURITY.md` gains the phase's update section, `ROADMAP.md` moves
  the phase to done.
- Merged with a merge commit whose subject names the phase and its
  one-line outcome.
