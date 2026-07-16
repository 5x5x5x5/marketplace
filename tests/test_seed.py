"""scripts/seed.py smoke: runs clean, idempotent, prints usable tokens."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_seed(db_url: str) -> str:
    env = os.environ | {
        "DATABASE_URL": db_url,
        "ADMIN_EMAIL": "admin@marketplace-demo.dev",
        "ADMIN_PASSWORD": "try-it-admin-password",
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
        "SMTP_HOST": "",
    }
    result = subprocess.run(
        [sys.executable, "scripts/seed.py"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def test_seed_runs_twice_and_prints_tokens(tmp_path: Path) -> None:
    db_url = f"sqlite+pysqlite:///{tmp_path}/seed.db"
    first = _run_seed(db_url)
    assert "rideshare" in first and "cleaning" in first
    for line in ("admin", "buyer", "seller"):
        assert line in first
    # second run: signup falls back to login, upserts no-op, fresh tokens print
    second = _run_seed(db_url)
    assert "Seeded" in second
