# Background agents: issue to pull request, automated review, weekly upkeep

> **Status: written, linted, not executed.** None of these workflows has run.
> They need a secret only the maintainer can add, and GitHub Actions was not
> available where they were written. They were validated with `actionlint`
> (with `shellcheck`), YAML parsing, and a check of the issue forms against
> GitHub's documented form schema. Treat the first runs as a trial on a
> low-stakes issue. Section 6 lists what is still unverified.

`docs/agent-setup.md` describes the interactive loop (skills, hooks,
reviewers). This document covers the asynchronous side: an agent that works
without anyone at the keyboard, and the controls that make that safe on a
public repository.

## 1. The pieces

| File | Trigger | What it does |
|---|---|---|
| `.github/ISSUE_TEMPLATE/bug.yml`, `task.yml`, `config.yml` | issue creation | Forms that are small specifications: observed/expected/reproduction/invariant for bugs; goal, checkable acceptance criteria, out of scope, and the governing skill for tasks. Blank issues are off. |
| `.github/pull_request_template.md` | PR creation | What and why, gate output pasted (not claimed), invariants touched, docs updated. |
| `.github/workflows/agent-implement.yml` | `agent:implement` label on an issue, or `@claude` in a comment on an issue or same-repo PR | A `gate` job proves the actor has write access; then the `implement` job runs the agent, which branches (`agent/...`), implements test-first, runs the gate, and opens a pull request. |
| `.github/workflows/agent-review.yml` | `pull_request` to `main` | The agent reads the diff, applies the spec-compliance reviewer always, the money-path reviewer and checklist for money/status changes, the asymmetry reviewer for views, templates and notification payloads, and posts comments. |
| `.github/workflows/maintenance.yml` | weekly cron, manual | Runs the full gate (SQLite and Postgres, hook self-tests, demo), reads the latest scheduled Security run, and opens or updates one `maintenance` issue if anything is red; closes it when green. No agent, no secrets. |
| `ci.yml` (`hooks` job) | push, PR | Runs `.claude/hooks/test_hooks.py`, which is outside the default `tests/` path. |

The action is `anthropics/claude-code-action`, pinned to
`4fa93a88de49bbef68b10eb15aabedf28ca0e1ed` (tag `v1.0.271`).

## 2. Security model

The threat: a public repository runs an agent holding repository credentials
on text that strangers can write (issue bodies, comments, PR descriptions,
diffs). The controls are layered so that no single mistake in the prompt is
the only thing between an attacker and the repository.

### Who can start a run

| Requirement | Enforced by |
|---|---|
| Only people with write access trigger the implementing agent. | (1) The `gate` job calls `GET /repos/{repo}/collaborators/{actor}/permission` and proceeds only for role `admin`, `maintain` or `write`; any other result, including an API error, fails closed. (2) The action independently checks write access for issue, comment and PR events. (3) Bot senders are excluded in the job `if`, so the agent's own comments cannot re-trigger it. Applying a label already needs triage rights; the permission check above is stricter. |
| An outside contributor's issue or comment never starts a run. | The `issues` trigger fires only on `labeled` with the exact label `agent:implement`; opening an issue starts nothing. A comment from a non-writer fails the `gate` check, and the `implement` job, which holds the API key, never starts (its `needs`/`if` depend on `gate`). `author_association` is deliberately not used: it can say `COLLABORATOR` for a read-only user. |
| A pull request from a fork never runs with secrets or write permissions. | `agent-review.yml` uses `pull_request`, under which GitHub withholds secrets and gives fork PRs a read-only token, and additionally skips the job unless `head.repo.full_name == github.repository`. `agent-implement.yml` on a PR comment looks up the PR's head repository in `gate` and refuses unless it is this repository, so a maintainer commenting on a fork PR cannot cause an agent to check out and execute fork code with the key. |
| No `pull_request_target`. | Nothing here uses `pull_request_target` or `workflow_run`, so there is no privileged run that could check out untrusted code. Do not add one. |
| Dependabot PRs. | Skipped by the review workflow (they have no secrets anyway). |

The `gate` job holds no secret and only reads. Its inputs reach the shell as
environment variables (`ACTOR`, `ISSUE_NUMBER`, `REPO`), never by
`${{ }}` interpolation into a script.

### Least privilege

`permissions: {}` at workflow level; each job grants only what it uses.

| Job | Permissions | Why |
|---|---|---|
| `implement.gate` | `contents: read`, `pull-requests: read` | Read the collaborator role and PR head repo. |
| `implement.implement` | `contents: write`, `pull-requests: write`, `issues: write`, `id-token: write` | Push the agent's branch, open the PR, comment, and let the action exchange an OIDC token (the action's documented set). It has no `workflows`, `actions: write`, `packages` or `security-events`. |
| `review.review` | `contents: read`, `pull-requests: write`, `id-token: write` | Read code, write comments. No `contents: write`. |
| `maintenance.gate` | `contents: read`, `issues: write`, `actions: read` | Run checks, read the Security run, file one issue. |

Beyond the workflow token, the Claude GitHub App token used by the action has
no `workflows` permission, so an agent cannot push changes under
`.github/workflows/` even if instructed to; the prompt also forbids it.
Only `ANTHROPIC_API_KEY` is exposed to the agent steps; no other repository
secret should be added to the repository (see section 3).

### Untrusted input

* The system prompt in `agent-implement.yml` and the prompt in
  `agent-review.yml` both state that issue text, comments, PR descriptions,
  diffs and file contents are data, not instructions, and that text asking the
  agent to change its rules is a reason to stop (implement) or a finding
  (review).
* The only event-derived values interpolated into the workflows are the
  repository name, integers (issue/PR numbers) and the default branch name.
  Titles, bodies, comments and branch names from a PR are never placed in
  `run:` scripts or prompts; the agent fetches them itself with `gh`.
* Tools are allow-listed through `claude_args`. The implementer gets file
  tools plus a short list of `uv run` gate commands, `git` add/commit/branch,
  `git push` of `HEAD` to `origin` only, and `gh pr create/view/diff`. It has
  no `curl`, no web tools, no `gh api`, no `gh workflow`, no `gh secret`, no
  merge or review commands, and no force-push, reset or rebase. The reviewer
  has read-only file tools, `git show/diff/log`, and `gh pr comment/diff/view`
  and inline comments: no Edit, Write, `gh pr review`, `gh pr merge`.
* The reviewer reads `CLAUDE.md` and the reviewer definitions from the base
  branch, because the head is what is being reviewed and a PR could otherwise
  edit its own rules. The action also restores its Claude configuration paths
  from the base branch on pull requests.
* Prompt injection is not eliminated by any of this; it is bounded. The
  worst an injected instruction can do is limited by the allowed tools, the
  token permissions, and branch protection below.

### "Cannot merge" is enforced by GitHub, not by the prompt

The prompts forbid merging, approving, rewriting history, and skipping or
deleting tests, and the tool lists omit the commands. Those are the first
line. The one that cannot be talked around is repository configuration:

* Branch protection (or a ruleset) on `main`: require a pull request, at
  least one approving review from a human (code owner), the CI jobs as
  required status checks (lint, typecheck, test-sqlite, test-postgres,
  migrations, hooks), no bypass, no force-push, no deletion, and "dismiss
  stale approvals".
* The review workflow can only comment. Its job is not a required check, and
  nothing in it can produce an approving review. Do not enable "Allow GitHub
  Actions to create and approve pull requests".
* Keep auto-merge disabled for the repository, or limited by the protection
  rules above; the prompts also forbid enabling it.
* A `CODEOWNERS` entry for `.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`
  and `SECURITY.md` with a human owner makes changes to the agent's own
  rules need human review.

Rules to the agent that no tool can enforce (never skip/disable/quarantine a
test to get green) are backed by CI running the same suite, and by the
spec-compliance review, which is told to flag any test that is added-skipped,
weakened or removed.

## 3. Setup the maintainer must do

1. **Install the Claude GitHub App** on this repository
   (https://github.com/apps/claude, or run `/install-github-app` in Claude
   Code). The workflows use the action's default app-token path, so no
   `github_token` input is set. If you prefer your own app, create it with
   read/write on Issues, Pull Requests and Contents only, and pass its token
   as `github_token`.
2. **Add the secret** `ANTHROPIC_API_KEY` (Settings, Secrets and variables,
   Actions, Repository secrets). `claude_code_oauth_token` also works if you
   change the input name. Use a key with a spend limit. To restrict it
   further, put it in an Environment (for example `agent`) with required
   reviewers and add `environment: agent` to the two jobs.
3. **Create the labels** `agent:implement` and `maintenance`.
4. **Set branch protection / a ruleset on `main`** as in section 2.
5. **Settings, Actions, General:** "Fork pull request workflows from outside
   collaborators" set to "Require approval for all outside collaborators";
   workflow permissions default to read-only; leave "Allow GitHub Actions to
   create and approve pull requests" off.
6. **Optionally** add `.github/CODEOWNERS` as described above.
7. Run a trial: file a small `task.yml` issue and apply the label; watch the
   run logs. Run `maintenance` once with "Run workflow".

Pull requests opened by the Claude app do not carry the `GITHUB_TOKEN`
loop-prevention restriction, so CI should run on them; confirm this on the
first run (section 6).

## 4. Worked example

1. A maintainer files a **Feature task**: goal "buyers can see the listing
   count per seller on the catalog page", acceptance criteria as checkboxes
   ("page source contains no payout or capacity", "asymmetry test added",
   "gate passes"), out of scope "no API changes", skill `endpoint-or-page`.
2. They apply the `agent:implement` label. `agent-implement.yml` starts;
   `gate` confirms the labeler has write access.
3. `implement` checks out the default branch, installs dependencies, and
   starts the agent with the issue as context. The agent reads `AGENTS.md`
   and `CLAUDE.md`, follows the `endpoint-or-page` skill, writes the failing
   test, implements, and runs the gate unpiped.
4. It pushes branch `agent/issue-N-...` and opens a pull request using the PR
   template, with the gate output pasted and the acceptance criteria ticked.
   If any is unmet it opens a draft and says which.
5. CI runs. `agent-review.yml` runs on the PR: it applies the
   spec-compliance reviewer and, because a template and view changed, the
   asymmetry reviewer, then comments with findings and a `Checked:` line.
6. The maintainer reads the review, asks for changes by commenting `@claude
   ...` (same write-access gate, same-repo head), and when satisfied approves.
   Branch protection requires that human approval and green checks; only then
   the maintainer merges. The agent never does.
7. Mondays, `maintenance.yml` runs the gate on `main`. If a new advisory or a
   flaky test turns something red, it opens or updates one `maintenance`
   issue; that issue can itself be handed to the agent with the label.

## 5. Costs and limits

`--max-turns` bounds each run (60 implement, 40 review), jobs have
`timeout-minutes`, and concurrency groups serialize runs per issue/PR (the
review run cancels when a newer commit arrives). A runaway loop costs at most
one job's budget. Review every run's logs while the setup is new; logs on a
public repository are public, so keep `show_full_output` off (its default).

## 6. What is not verified

* No workflow has executed. Every behavior in this document is the intended
  behavior from reading the action's documentation, not an observation.
* Whether `gh pr create` works inside the action's environment with the
  app token (the action's own documentation says it by default only pushes a
  branch and links a pre-filled PR page). If it fails, the agent leaves the
  link in its comment and the maintainer opens the PR; nothing else breaks.
* Whether the action's checkout/git setup works with `persist-credentials:
  false` (the action's examples use the default). If pushes fail with
  authentication errors, remove that line from `agent-implement.yml` only.
* Whether CI triggers on pull requests created by the app (expected, since
  the push is by an app token rather than `GITHUB_TOKEN`).
* The `maintenance` workflow's "security" result depends on the Security
  workflow's scheduled run having completed earlier the same day; if GitHub
  delays that schedule it reports the previous week's result.
* The reviewer's routing between checklists is the agent's judgment guided
  by the prompt; there is no deterministic path filter, on purpose, because
  source paths are being reorganized.
