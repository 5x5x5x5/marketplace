---
name: migration
description: Add or change an Alembic migration in this repo so it runs from an empty database on both SQLite and Postgres, keeps a single head, imports nothing from the app, and names constraints the way Postgres already named them. Use whenever an entity in src/marketplace/entities.py gains or loses a table, column, index, or constraint, or when touching anything under migrations/.
---

# Adding a migration

The test suite builds its schema with `Base.metadata.create_all`, not with
Alembic, so a broken migration passes every app test. That is how the
listings migration shipped with a bare `op.add_column(..., sa.ForeignKey(...))`
that SQLite cannot run, and `alembic upgrade head` failed from an empty
SQLite database until a later fix. The checks below are what catches that
class now.

## Steps

1. **Change the entity first** (`entities.py`), using the existing helpers:
   `UTCDateTime` for datetimes, the `native_enum=False, length=32` enum helper,
   `Numeric(12, 2)` for money.
2. **Base on the current head.** `uv run alembic heads` must print exactly one
   revision; set `down_revision` to it. Two parallel branches that both add a
   revision produce two heads; the later one re-chains onto the other
   (`tests/test_migrations.py::test_single_migration_head` fails otherwise).
3. **Autogenerate against a scratch Postgres DB at head**, so generated names
   are Postgres's:
   ```bash
   .claude/skills/gate/gate.sh --pg-only   # with GATE_KEEP_DB=1 to keep the DB it prints
   DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/<that db> \
     uv run alembic revision --autogenerate -m "short_snake_name"
   ```
   Then read and edit the file by hand; autogenerate is a draft.
4. **Make it SQLite-safe.** SQLite cannot `ALTER` a table to add or drop a
   constraint, or alter a column type. Put those operations in
   `with op.batch_alter_table("<table>") as batch_op:` (copy-and-move on
   SQLite, plain `ALTER`s on Postgres). Adding a plain nullable column or
   creating an index needs no batch.
5. **Name every constraint you create or drop inside a batch**, using the name
   Postgres generates by default, because there is no `naming_convention` on
   the metadata: foreign key `<table>_<column>_fkey`, unique
   `<table>_<column>_key`, primary key `<table>_pkey`, index `op.f("ix_<table>_<column>")`.
   Matching Postgres's default means a database migrated before and after a
   repair ends up identical; an unnamed constraint inside a batch cannot be
   dropped later on SQLite. CHECK constraints follow the house prefix
   `ck_<table>_<rule>` (see `ck_adjustments_amount_nonneg`).
6. **No app imports.** `migrations/versions/*` must not import `marketplace.*`
   (the edit guard hook denies it). A migration has to replay the same way
   after the models it was generated from have moved on. `UTCDateTime` is
   written as `sa.DateTime(timezone=True)`; `migrations/env.py` renders it
   that way automatically. Enums are inlined as
   `sa.Enum("a", "b", name="...", native_enum=False, length=32)`.
7. **Write `downgrade()`** as the exact inverse, batch blocks included, in
   reverse order. The round trip runs it.
8. **Verify on both engines:**
   ```bash
   uv run pytest tests/test_migrations.py -q   # SQLite: up, down to base, up; single head
   .claude/skills/gate/gate.sh --pg            # Postgres round trip, alembic check, suite on migrated schema
   ```
   `alembic check` failing means the models and migrations disagree (a column
   in one and not the other); fix the migration, not the check.

## Things that need no migration

Adding a value to an existing enum: enums are non-native `VARCHAR(32)` with
no CHECK constraint, so a new value only has to fit in 32 characters.

## Editing a migration that is already merged

Don't, unless it cannot run at all. Databases have already applied it, so a
change forks old installs from new ones; a fix goes in a new revision. The
edit guard asks before any change to `upgrade()` of a revision that exists on
the default branch. The legitimate exception is the listings repair: it moved
operations into batch mode with the constraint name Postgres had already
generated, so existing Postgres databases were unaffected. Say so in the
commit body when you take that path.

## Count

`CLAUDE.md`'s non-goals section states the migration count; update it in the
same change if your phase adds one.
