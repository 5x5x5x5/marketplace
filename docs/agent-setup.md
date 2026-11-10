# Agent setup: what is in `.claude/` and why

This repository was built almost entirely by coding agents working from
written specs and plans. For most of that time the workflow lived in the
heads of whoever ran the sessions and in personal configuration. This
document describes the checked-in version: what each piece does, which past
failure it encodes, and what was left out on purpose.

The design rule throughout: **a few repo-specific artifacts, each tied to a
failure this codebase actually had**. Nothing here would be equally true of
any Python repository; generic advice ("write tests", "use type hints") is
left to the agent's defaults.

## The layers

| Layer | Loaded | Good for |
|---|---|---|
| `CLAUDE.md` | every session, always | invariants that must hold on every edit |
| `AGENTS.md` | by agents that don't read `CLAUDE.md` | orientation; points at `CLAUDE.md` |
| Skills (`.claude/skills/`) | on demand, when the task matches the description | procedures and checklists for one kind of change |
| Subagents (`.claude/agents/`) | when delegated to | narrow reviews and single-task implementation in a fresh context |
| Hooks (`.claude/settings.json`) | on every matching tool call | rules cheap enough to check mechanically |
| Tests and the gate | on every gate run | everything that can be asserted about the code itself |

Each rule lives at the cheapest layer that can enforce it. A rule a hook can
check from a single edit doesn't need to be prose; a procedure only relevant
to migrations doesn't need to sit in every session's context.

## Skills

| Skill | Purpose | What it would have prevented |
|---|---|---|
| `gate` + `gate.sh` | The gate on SQLite and, with `--pg`, on a scratch Postgres DB: migration up/down/up, `alembic check`, the suite on the migrated schema. | A migration that only ran on Postgres passed every test (the suite uses `create_all`). Postgres-only race tests skipped silently on SQLite. Piped gate output reporting a red run as green. |
| `migration` | SQLite batch mode, Postgres-default constraint names, no app imports, one head, inverse downgrade. | The listings migration's `add_column(..., ForeignKey)` failing from an empty SQLite DB; parallel branches producing two heads. |
| `money-path` | 21-item checklist for anything that moves money or changes status, each item tied to its incident. | The stale re-lock that let a cancel and a complete both win (seller paid, buyer refunded); the cancel/webhook lock-order deadlock; the partial-reversal webhook mapping that would have double-paid on retry; the dispute double-booking during a lock-free window. |
| `endpoint-or-page` | Which view family each route returns, identity from the principal, catalog reads through `catalog.py`, autoescape, the asymmetry tests to add. | The seller completion receipt returning the admin ledger model (buyer price and margin to every seller); a seller view leaking `buyer_id`; catalog visibility re-implemented twice and drifting. |
| `pricing-extension` | Adding an adjuster or strategy while keeping the core pure, the fee-aware floor, and operator enablement through admin endpoints. | A rule that reads the DB or the clock inside the pure core; a strategy comparing the gross spread instead of the net-of-fees floor; the two buyer-price construction sites drifting. |
| `feature-phase` (+ spec and plan templates) | The loop every phase followed: spec, plan with Files/Interfaces/Steps, test-first tasks, narrow whole-branch review, fix wave, try-out, docs, merge. | Losing the loop between sessions; tasks that later tasks can't build on because interface names drifted. |
| `tryout` + `probe.py` | Drive a running server over real HTTP; record findings in the findings-doc format. | Findings F1, F2, F2b, F5: all invisible to `TestClient`, all found by driving a live server after green suites and clean reviews. The probe was checked against a deliberately reintroduced receipt leak and flagged it. |

Skill bodies give the reason with each rule, because an agent that knows
why a rule exists applies it to cases the rule didn't list.

## Subagents

| Agent | Tools | Job |
|---|---|---|
| `task-implementer` | read, edit, bash, skills (preloads `gate`) | One plan task, test first, gate, commit only listed files, report with the red output quoted. |
| `spec-compliance-reviewer` | read-only | Does the diff do what the plan task said, no more, no less; tests not weakened. |
| `money-path-reviewer` | read-only | Races, stale re-reads, lock order, double effect, idempotency keys, commit ordering, webhook replay, ledger immutability. |
| `asymmetry-reviewer` | read-only | The other side's price or identity in a response, payload, page, or error; hand-built dicts; missing asymmetry tests. |

Why narrow reviewers: the git history shows that the defects that reached a
final review were found by reviewers looking for one class of bug, after
every task had already passed a general review. A reviewer told to check
everything checks style. Each prompt lists the concrete incidents of its
class and requires findings as `severity / where / rule / scenario /
confidence / fix`, plus a `Checked:` line stating coverage, so the output can
be triaged and an empty result can be told apart from a shallow one.

Reviewers have no Edit or Write tool, and their Bash goes through a
frontmatter `PreToolUse` hook (`.claude/hooks/readonly_bash.py`) that only
allows inspection commands (git diff/log/show, rg, sed -n, cat, find without
actions). Frontmatter hooks only run once the workspace is trusted; until
then the normal permission prompts still apply.

## Hooks

All hook scripts are standard-library Python or bash, finish in well under a
second (the session-start sync excepted), and **fail open**: an exception in
a script exits 0 with no decision, so a broken hook never blocks a session.
`uv run pytest .claude/hooks/test_hooks.py` runs each one against sample
tool-input JSON.

| Hook | Event | Blocks | Why | Override |
|---|---|---|---|---|
| `guard_edit.py` | PreToolUse, Edit/Write | `import stripe` outside `payments/stripe_provider.py`; `marketplace` imports in `migrations/versions/`; `|safe` or `autoescape false` in templates and `autoescape=False` in Python | Each is a one-line edit that breaks a `CLAUDE.md` non-negotiable (provider port, replayable migrations, the XSS boundary) and is detectable from the edit alone. | Deny rules have no switch; change the script in a reviewed commit if one is ever wrong. |
| `guard_edit.py` | same | **Asks** (doesn't deny) before a change to `upgrade()` of a migration that exists on the default branch | Databases have already run it. Asking rather than denying leaves room for the legitimate repair (the listings SQLite fix edited a merged migration on purpose). Downgrade and docstring edits pass. | Approve the prompt, or launch with `MARKETPLACE_ALLOW_MIGRATION_EDIT=1`. In unattended modes an ask becomes a denial with the reason shown. |
| `format_lint.py` | PostToolUse, Edit/Write on `.py` | Nothing (feedback only) | `ruff check --fix` + `ruff format` on the edited file, remaining findings shown to the agent. It never removes unused imports: the hook used during the build did, deleting an import added one edit before its first use, and every plan carried a warning about it. | Not needed; it only formats. |
| `session_start.sh` | SessionStart (startup, resume) | Nothing | In cloud sessions (`CLAUDE_CODE_REMOTE=true`) runs `uv sync --frozen` so the gate works immediately, and reports whether Postgres is reachable so the agent knows whether the Postgres gate can run. No-op locally. | Not needed. |

`permissions` in `.claude/settings.json` allows the routine gate commands
(`uv run ruff/pyright/pytest`, the demo, read-only Alembic commands,
`gate.sh`). Read-only shell and git commands are already allowed by the
tool itself. It denies `docker compose down -v` (the Postgres volume may be
shared between checkouts; an earlier plan forbade it in prose) and reading
`.env` (it can hold a live payment-provider key; the seed and demo scripts
already pin those variables empty for the same reason).

## Deliberately left out

- **A per-edit pyright hook.** Strict pyright takes several seconds; on
  every edit that's a tax on each step. It runs in the gate.
- **Hooks for rules that need context**: "identity never from a request
  body", "flush before returning ORM objects", "re-read after a lock". A
  regex would either miss real cases or block legitimate edits. These live
  in skills and in the reviewers, where the whole function is read.
- **A planner subagent.** Writing a spec and plan needs the maintainer's
  decisions in the loop; it is done in the main session with the
  `feature-phase` skill and its templates.
- **A general "code reviewer" agent.** Built-in review tooling covers general
  review; the repo-specific value is in the narrow ones.
- **Stop hooks that force the gate before finishing.** The gate takes over a
  minute on SQLite and longer with Postgres; forcing it on every stop would
  punish exploratory and read-only sessions. The skills and the implementer
  require it where it matters.
- **Git-push or commit permissions.** Left to each person's own settings.
- **Model choices in agent definitions.** Every agent inherits the session's
  model so the setup doesn't date.

## Keeping it honest

When a new class of bug is found (a try-out finding, a final-review
Critical), add it in the same change as the fix: the incident and rule to
the relevant skill and reviewer, a hook only if the rule is detectable from
one edit, and an invariant to `CLAUDE.md` only if it must hold on every edit.
