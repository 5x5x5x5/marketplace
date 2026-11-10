"""Migrations must build the schema from an empty database on SQLite too.

The suite itself runs on `Base.metadata.create_all`, so a migration that only
works on Postgres passes every other test here — that is how the listings
migration shipped with a bare `add_column(..., ForeignKey)` SQLite cannot
ALTER. This runs the real Alembic chain in a subprocess (the app's `settings`
singleton is already bound to the test database in this process) against a
scratch SQLite file: up, all the way down, and up again.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )


def test_migrations_round_trip_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}"
    for step in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        result = _alembic(url, *step)
        assert result.returncode == 0, f"alembic {' '.join(step)} failed:\n{result.stderr}"


def test_single_migration_head() -> None:
    result = _alembic("sqlite+pysqlite://", "heads")
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected one migration head, got: {heads}"
