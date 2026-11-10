## What and why

<!-- What changed, and the reason. Link the issue: "Closes #123". -->

## How it was verified

<!-- Paste the output of the gate, not a claim that it passed.
     .claude/skills/gate/gate.sh [--pg], or:
     uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -->

```
```

- [ ] Gate passes on SQLite
- [ ] Suite run on Postgres (required for migrations, locking, payments, webhooks)
- [ ] New behavior has a test that failed before the change; no test skipped, weakened, or deleted

## Invariants touched

<!-- Name the CLAUDE.md non-negotiables this change is near and how it keeps them
     (identity from the principal, role-scoped views, Decimal money, commit in
     CommitRoute, ...). Write "none" if truly none. -->

## Docs updated

<!-- CLAUDE.md, SECURITY.md, ROADMAP.md, docs/, or "no doc impact" with the reason. -->
