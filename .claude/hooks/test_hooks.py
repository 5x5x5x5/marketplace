"""Exercise the hook scripts the way Claude Code runs them: JSON on stdin.

    uv run pytest .claude/hooks/test_hooks.py -q

Outside the app suite's testpaths on purpose (these test tooling, not the
marketplace), so `uv run pytest` alone doesn't collect them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent
REPO = HOOKS.parent.parent
MERGED_MIGRATION = REPO / "migrations/versions/87aec6283afd_ops_indexes.py"


def run(script: str, payload: object, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    cmd = ["bash", str(HOOKS / script)] if script.endswith(".sh") else [sys.executable]
    if not script.endswith(".sh"):
        cmd.append(str(HOOKS / script))
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def decision(stdout: str) -> str | None:
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


def edit(path: Path, old: str, new: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
    }


def write(path: Path, content: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": content},
    }


# ---------- guard_edit.py ----------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (write(REPO / "src/marketplace/api.py", "import stripe\n"), "deny"),
        (write(REPO / "tests/test_x.py", "from stripe import error\n"), "deny"),
        (edit(REPO / "src/marketplace/payments/stripe_provider.py", "a", "import stripe"), None),
        (
            write(REPO / "migrations/versions/new_rev.py", "from marketplace.entities import X\n"),
            "deny",
        ),
        (write(REPO / "migrations/env.py", "from marketplace.entities import Base\n"), None),
        (edit(REPO / "src/marketplace/templates/listing.html", "a", "{{ l.title|safe }}"), "deny"),
        (edit(REPO / "src/marketplace/templates/base.html", "a", "{% autoescape false %}"), "deny"),
        (edit(REPO / "src/marketplace/pages.py", "a", "Environment(autoescape=False)"), "deny"),
        (write(REPO / "src/marketplace/models.py", "x = 1\n"), None),
        (write(Path("/tmp/elsewhere/import_stripe.py"), "import stripe\n"), None),
    ],
)
def test_guard_hard_rules(payload: dict[str, object], expected: str | None) -> None:
    code, out, _ = run("guard_edit.py", payload)
    assert code == 0
    assert decision(out) == expected


def _on_default_branch(path: Path) -> bool:
    rel = path.relative_to(REPO).as_posix()
    for ref in ("origin/HEAD", "origin/main", "main", "master"):
        probe = subprocess.run(
            ["git", "-C", str(REPO), "cat-file", "-e", f"{ref}:{rel}"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return True
    return False


needs_merged = pytest.mark.skipif(
    not _on_default_branch(MERGED_MIGRATION), reason="needs the migration on the default branch"
)


@needs_merged
def test_guard_asks_before_changing_merged_upgrade() -> None:
    payload = edit(MERGED_MIGRATION, '["provider_account_id"],', '["provider_account_id", "x"],')
    code, out, _ = run("guard_edit.py", payload)
    assert code == 0
    assert decision(out) == "ask"


@needs_merged
def test_guard_allows_downgrade_and_docstring_edits_on_merged_migration() -> None:
    down = edit(
        MERGED_MIGRATION,
        'op.drop_index(op.f("ix_payouts_provider_transfer_id"), table_name="payouts")',
        'op.drop_index(op.f("ix_payouts_provider_transfer_id"), table_name="payouts")  # c',
    )
    doc = edit(MERGED_MIGRATION, '"""ops indexes', '"""ops indexes (seller account, transfer)')
    for payload in (down, doc):
        code, out, _ = run("guard_edit.py", payload)
        assert (code, decision(out)) == (0, None)


@needs_merged
def test_guard_migration_override_env() -> None:
    payload = edit(MERGED_MIGRATION, '["provider_account_id"],', '["provider_account_id", "x"],')
    code, out, _ = run("guard_edit.py", payload, {"MARKETPLACE_ALLOW_MIGRATION_EDIT": "1"})
    assert (code, decision(out)) == (0, None)


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Edit"}'])
def test_guard_fails_open_on_bad_input(stdin: str) -> None:
    code, out, _ = run("guard_edit.py", stdin)
    assert (code, out) == (0, "")


# ---------- readonly_bash.py ----------


def bash(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize(
    "command",
    [
        "git diff main...HEAD -- src/marketplace/api.py",
        'rg -n "buyer_price|margin" src/marketplace | head -20',
        "git -C /repo log --oneline -20",
        "sed -n 1,80p src/marketplace/api.py",
        "git diff 2>/dev/null | wc -l",
        "cat SECURITY.md 2>&1 | head",
        "git show HEAD --stat && git status --short",
        "find src -name '*.py'",
        "uv run alembic heads",
        "git branch -a",
    ],
)
def test_readonly_allows_inspection(command: str) -> None:
    code, out, _ = run("readonly_bash.py", bash(command))
    assert (code, decision(out)) == (0, None)


@pytest.mark.parametrize(
    "command",
    [
        "echo x > notes.txt",
        "sed -i s/a/b/ src/marketplace/api.py",
        "git commit -am wip",
        "git -c core.pager=sh log",
        "git diff --output=/tmp/x",
        "git branch -D main",
        "uv run pytest -q",
        "uv run alembic upgrade head",
        "rm -rf src",
        "find . -name '*.pyc' -delete",
        "cat $(echo api.py)",
        "git log | tee out.txt",
        'python3 -c \'open("x","w")\'',
    ],
)
def test_readonly_denies_everything_else(command: str) -> None:
    code, out, _ = run("readonly_bash.py", bash(command))
    assert (code, decision(out)) == (0, "deny")


def test_readonly_fails_open_on_bad_input() -> None:
    code, out, _ = run("readonly_bash.py", "garbage")
    assert (code, out) == (0, "")


# ---------- format_lint.py ----------

ruff_available = pytest.mark.skipif(
    not ((REPO / ".venv/bin/ruff").is_file() or shutil.which("ruff")), reason="ruff not installed"
)


def _project(tmp_path: Path, source: str) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F", "I"]\n')
    target = tmp_path / "mod.py"
    target.write_text(source)
    return target


def post(path: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
    }


@ruff_available
def test_format_lint_formats_and_keeps_unused_import(tmp_path: Path) -> None:
    target = _project(tmp_path, "import os\nx=1\n")
    code, _, err = run("format_lint.py", post(target))
    assert code == 0, err
    assert target.read_text() == "import os\n\nx = 1\n"


@ruff_available
def test_format_lint_reports_remaining_findings(tmp_path: Path) -> None:
    target = _project(tmp_path, "x = undefined_name\n")
    code, _, err = run("format_lint.py", post(target))
    assert code == 2
    assert "F821" in err


def test_format_lint_ignores_non_python(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("x")
    assert run("format_lint.py", post(target))[0] == 0
    assert run("format_lint.py", "garbage")[0] == 0


# ---------- session_start.sh ----------


def test_session_start_is_a_noop_locally(tmp_path: Path) -> None:
    env = {"CLAUDE_CODE_REMOTE": "", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    assert run("session_start.sh", {"source": "startup"}, env) == (0, "", "")


def test_session_start_skips_dirs_without_a_lockfile(tmp_path: Path) -> None:
    env = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    assert run("session_start.sh", {"source": "startup"}, env) == (0, "", "")
