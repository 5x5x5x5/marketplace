# How this was built

This repository was built almost entirely by coding agents, directed by one
maintainer. This document describes the process, points at the evidence
for each claim, and says where the process failed. It is written for
someone deciding how much to trust the result, or thinking about working
this way themselves.

The short version: the agents wrote the code, and most of the engineering
effort went into everything around the code. That means written invariants,
specifications before implementation, narrow adversarial review, tests that
had to fail first, a live try-out against a real server, and checks that
turn prose rules into machine failures. Each of those exists because an
earlier, lighter process let a real bug through.

## The loop

Every feature phase followed the same sequence. It is written down as a
skill an agent can follow (`.claude/skills/feature-phase/`), but it was a
habit before it was a file.

1. **Design spec.** What the phase does, what it deliberately does not do,
   the data model, the failure modes, and the invariants it adds. One per
   phase, in `docs/superpowers/specs/`.
2. **Implementation plan.** The spec broken into tasks small enough to
   build and review one at a time, each naming its files, its tests, and
   its commit. In `docs/superpowers/plans/`.
3. **Build, task by task, test first.** The failing test is written and
   seen to fail before the code that makes it pass. The full gate (lint,
   format, strict type checking, the suite) runs after every task.
4. **Narrow adversarial review.** Not one general reviewer but several
   with a single job each: does the diff do what the plan's task said; can
   money be double-charged, double-paid or stranded; can one side of the
   marketplace see the other side's price. Their definitions are in
   `.claude/agents/`.
5. **Fix wave.** Review findings are fixed with a regression test each.
   These show up in the history as commits titled "final review" and "fix
   wave".
6. **Live try-out.** A real server, real HTTP, a person or a script
   driving the whole lifecycle, looking for what the in-process test client
   cannot show.
7. **Merge**, with the phase recorded in `ROADMAP.md` and any new rule
   recorded in `CLAUDE.md`.

The maintainer's part was deciding what to build and what to leave out,
approving specs, ruling on judgment calls the specs left open, and reading
the findings. The decisions that shaped the project are human ones: prices
are set by the platform and never by sellers; the two sides never see each
other's numbers; a list of things (rate limiting, admin roles, social
login) is deliberately left to whoever forks the template.

## What each layer caught

The useful question about a process like this is which layer catches which
kind of mistake. The history gives real answers.

**Review caught design-level mistakes early, and missed things outside the
diff.** The fix waves that followed phase reviews closed, among others, a
path where a late payment success could resurrect a voided payment on a
cancelled job, and a chunked-request case that could poison an idempotency
key. But the worst
leak in the project's history, a seller's completion receipt that included
the buyer's price and the platform's margin, sat in the code through
several reviewed phases. The reviews were scoped to each phase's diff, and
the leak predated them. The write-up of that finding
(`docs/tryout-findings-2026-07-16.md`, F1) includes a section titled "why
every review missed it".

**The live try-out caught what the test client hid.** Driving a real
server found five things in a day. The most important was that the
database commit ran after the response had already been sent, so a client
that immediately followed a write with a read could miss its own write
(F2). The 270 tests that existed then could not see this, because the
in-process test client does not return until the whole request, teardown
included, has finished. The fix
(`db.CommitRoute`) is now an invariant with an import-time check. The
try-out also found a cancel racing a completion and refunding a job that
had been completed and paid out (F5), which came down to one missing
`populate_existing` on a re-read after taking a lock.

**A measured baseline caught capacity problems that reading the code had
only predicted.** A load test showed the server completing nothing at all
under 100 concurrent requests, because a 15-connection pool sat behind a
40-thread pool and every request queued for 30 seconds. The fix and the
before/after numbers are in `docs/capacity-2026-11.md`.

**A targeted audit caught bugs nothing else was looking for.** After the
template was "done", an audit took five classes of money-path bug and
checked every payment-provider call and every pairing of row locks against
them (`docs/money-path-audit-2026-11.md`). It found two real bugs in code
that had passed its phase reviews and its tests. In one, a dispute
clawback of a seller's whole payout was reported back by the provider in
the same shape as a failed transfer, the handler marked the payout failed,
and an admin "retry payout" would have paid the seller a second time. No
test covered it because nobody had thought of that sequence. The audit
also recorded six hazards it chose not to fix, with reasons.

**Executable checks catch the rules that are easy to forget.** `CLAUDE.md`
lists nearly fifty invariants. An agent that has read them can still violate one
in a long session. So the statically checkable ones are tests
(`tests/test_architecture.py`) and Semgrep rules (`.semgrep/`), each with a
failure message that names the rule and the fix. When those tests were
written, 27 violations were injected one at a time to confirm each check
actually fails. The invariants that remain prose are behavioral ones a
static check cannot decide (replay safety, taking the right lock at the
right site, net-of-fees comparison); those rely on ordinary tests and on
review.

**Boring checks caught boring bugs.** A migration that could not run on
SQLite from an empty database shipped because the test suite builds its
schema directly from the models and never ran the migrations. One test
that runs the migration chain up, down and up again closes that
(`tests/test_migrations.py`). A dependency audit, the first time it was
run, reported twenty published advisories in six packages.

## The reviewer evaluation

Sixteen bugs, fourteen of them from this repository's own history and two
synthetic, were re-introduced one at a time as small, plausible changes
with innocent commit messages, alongside four correct changes as controls
(`evals/`). For each, the question was which layer of defense notices.

| Layer | Seeded bugs caught |
|---|---|
| Test suite on SQLite | 10 of 16 |
| Test suite on Postgres | 12 of 16 |
| Architecture tests | 4 of 16 |
| Semgrep rules | 1 of 16 |
| Reviewer agent reading only the diff (one run) | 16 of 16 |

No control change tripped any layer, the reviewer included.

The mechanical columns are the solid part. Two bugs passed every
mechanical layer (a notification payload shared between buyer and seller,
and a charge key derived from the offer instead of the job), so those
classes have no test or static check today. The regression tests for two
historical concurrency bugs only fail on Postgres, which is why CI runs
both engines. One mutation makes a Postgres test hang instead of fail, and
the suite has no per-test timeout.

The reviewer column needs more care than its number suggests. The bugs are
ones the repository already knows about: their rules are written in
`CLAUDE.md` and in the reviewer checklists, and the reviewer can read both.
Several diffs delete the very comment that explains the hazard. It is one
run per case, graded by one grader who knew the answers. So this shows
that the reviewers reliably apply rules the project has written down,
which is worth having, and it does not show that they find new kinds of
bug. The audit above is the evidence for that, and there the reviewers and
tests had both passed the code. The run's caveats and follow-ups are in
`evals/reviewer/results/2026-11-10/NOTES.md`.

The habit this is meant to support is simple: every real bug becomes a
case, and a guard test (`tests/test_reviewer_eval_cases.py`) keeps the
cases applying as the code moves.

## The overhaul, as a worked example of parallel agents

The last stretch of work (phase 13 in `ROADMAP.md`) was done differently
from the earlier phases, and is the best illustration here of agents
working in parallel.

One coordinating agent held the plan and the main branch. It wrote a brief
for each piece of work and handed it to a worker agent in its own git
worktree and its own test database, so workers could not disturb each
other. Up to five ran at once. A shared rules file told every worker the
same things: read the invariants first, write the failing test first, run
the gate on both database engines, do not edit the shared documents, and
end with a report that says what was skipped or uncertain. The coordinator
read each report, reviewed and merged the branch, ran the gate on the
merged result, and resolved what the merge broke.

What the workers did, each as its own merge commit:

| Work | What it was |
|---|---|
| Load-test harness | Browse, lifecycle and login scenarios with a runner that records its conditions |
| CI and security tooling | Workflows, Semgrep rules with a test corpus, architecture tests, pre-commit, a `justfile` |
| Performance | Pool sizing with load shedding, SQL pagination, a bounded quote path, housekeeping off read paths |
| Webhook outbox | Persisted failures, retry with backoff, dead-letter, and a replay-safety audit of every handler branch |
| Agent configuration | Skills, reviewer subagents, hooks, `AGENTS.md` |
| Services split | A 2,800-line module turned into thin routers and a service layer, with an identical OpenAPI document before and after |
| Money-path audit | The two bugs above |
| Ops MCP server | The admin API as tools for an operating agent |
| Background agents | Issue-to-pull-request, pull-request review, weekly upkeep |
| Documentation | README, roadmap, security notes and a deployment guide, checked against the code |
| Booking and payment pages | The buyer and seller flows in the browser, with session-bound CSRF tokens, built from its own spec and plan and driven in a real browser |
| Reviewer evaluation | Sixteen past bugs re-seeded as review cases, with the mechanical layers measured |

Some things worth knowing about how that went:

- **The brief is the work.** A worker starts with nothing but its brief
  and the repository. Briefs that explained why the work mattered and what
  made it hard produced better results than briefs that listed steps. Each
  brief also said what "done" meant and what to report.
- **Parallel work breaks at the seams, and the gate finds it.** Two
  workers each added a database migration on the same parent, giving the
  chain two heads; a test that checks for a single head failed on merge
  and the chain was re-linked. One worker changed a pagination helper's
  signature while another wrote a new caller of the old one; strict type
  checking failed on merge. Two money-path fixes were written against a
  module that another worker was, at the same time, splitting into pieces;
  they were re-applied by hand in the new locations, and their tests, which
  go through HTTP, passed unchanged.
- **Workers were told to report doubts, and did.** Worker reports flagged
  a direct mail send that contradicted a written rule (the rule was
  amended, because the exception is justified), database work running on
  the event loop inside a middleware (fixed), and the double-payout lead
  that the audit then confirmed. A separate worker reported that one of
  its own claims could not be verified and marked it so.
- **Not everything was verified, and the documents say which.** The GitHub
  workflows have never run: they need a secret only the maintainer can
  add. The Stripe card-entry flow has not been exercised against a live
  account. The load numbers come from one pair of runs on one shared
  machine. Each of these is stated where a reader would look for it.

## What this demonstrates, by topic

The pieces of this repository map onto the topics usually covered under
"AI-assisted software development":

| Topic | Where it is in this repo |
|---|---|
| Context engineering | `CLAUDE.md` (invariants with reasons), `AGENTS.md`, briefs that explain purpose |
| Spec-driven development | `docs/superpowers/specs/` and `plans/`, one pair per phase |
| Agent skills | `.claude/skills/`: the gate, migrations, money-path changes, endpoints and pages, pricing extensions, a feature phase, the try-out |
| Customizing the agent and the repository | Hooks that format edits and stop cheap rule violations; narrow reviewer subagents; a task implementer |
| Agent-ready codebases | Invariants as tests, a fast gate, both database engines in CI, migrations tested from empty, small modules with enforced layering |
| Agentic code review | Reviewer definitions by failure class, a pull-request review workflow, and an evaluation of the reviewers against this repo's own past bugs (`evals/`) |
| Security | Semgrep rules for the security invariants, dependency and secret scanning, a written threat model, and the prompt-injection surface of agent tooling treated as a real risk |
| Tools for agents (MCP) | `src/marketplace_mcp/`, with writes off by default and money-moving decisions left out |
| Background agents | `.github/workflows/agent-*.yml` and `docs/background-agents.md` |
| Measuring instead of asserting | `loadtest/`, `docs/capacity-2026-11.md`, `evals/` |

## Limits of this account

- The process described is what the history and the documents show. Nobody
  measured how long things took or what they cost, so there are no
  productivity claims here.
- The bugs listed are the ones that were found. The audit's premise, that
  reviewed and tested code still had a double-payout path, applies to the
  code as it stands now.
- A template has no users. Nothing here has been run in production, and
  `ROADMAP.md` lists what is known to be unfinished.
