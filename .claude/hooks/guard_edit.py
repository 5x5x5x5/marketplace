#!/usr/bin/env python3
"""PreToolUse guard for Edit/Write: stop edits that break a hard repo rule.

Each rule is cheap to detect from the tool input alone and maps to a
non-negotiable in CLAUDE.md:

- deny  `import stripe` / `from stripe` outside payments/stripe_provider.py
        (providers are reached only through payments/port.py)
- deny  `marketplace` imports in migrations/versions/* (migrations must not
        import app internals; they must replay identically years later)
- deny  `|safe` or `autoescape false` in templates, `autoescape=False` in
        Python (Jinja autoescape is the XSS boundary for seller content)
- ask   a change to upgrade() of a migration that already exists on the
        default branch (databases have already run it; a fix belongs in a new
        revision unless the old one cannot run at all, as with the SQLite
        batch-mode repair of the listings migration)

Override: the migration rule asks rather than denies, so a person can approve
a legitimate repair; set MARKETPLACE_ALLOW_MIGRATION_EDIT=1 in the environment
Claude Code was launched from to skip the prompt (unattended runs). The deny
rules have no switch: if one is ever wrong, change this file in a reviewed
commit.

Fails open: any error inside this script exits 0 without a decision, so a
broken hook never blocks a session. Standard library only.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STRIPE_HOME = "src/marketplace/payments/stripe_provider.py"
STRIPE_IMPORT = re.compile(r"^\s*(import\s+stripe\b|from\s+stripe\b)", re.MULTILINE)
APP_IMPORT = re.compile(r"^\s*(import\s+marketplace\b|from\s+marketplace\b)", re.MULTILINE)
TEMPLATE_UNSAFE = re.compile(r"\|\s*safe\b|autoescape\s+false", re.IGNORECASE)
PY_AUTOESCAPE_OFF = re.compile(r"autoescape\s*=\s*False")
DEFAULT_REFS = ("origin/HEAD", "origin/main", "main", "master")


def repo_root(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / "src" / "marketplace").is_dir() and (parent / "migrations").is_dir():
            return parent
    return None


def new_text(tool: str, tool_input: dict[str, object]) -> str:
    """Only the text the edit introduces; existing content is not re-judged."""
    key = "content" if tool == "Write" else "new_string"
    value = tool_input.get(key, "")
    return value if isinstance(value, str) else ""


def resulting_file(tool: str, tool_input: dict[str, object], current: str) -> str:
    if tool == "Write":
        content = tool_input.get("content", "")
        return content if isinstance(content, str) else ""
    old = str(tool_input.get("old_string", ""))
    new = str(tool_input.get("new_string", ""))
    if tool_input.get("replace_all"):
        return current.replace(old, new)
    return current.replace(old, new, 1)


def upgrade_source(source: str) -> str | None:
    """Source text of the module-level upgrade(), or None if absent/unparseable."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return ast.get_source_segment(source, node)
    return None


def on_default_branch(root: Path, rel: str) -> str | None:
    """The ref name if `rel` exists on the default branch, else None."""
    for ref in DEFAULT_REFS:
        probe = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{ref}:{rel}"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        if probe.returncode == 0:
            return ref
    return None


def decide(decision: str, reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def check(payload: dict[str, object]) -> tuple[str, str] | None:
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input")
    if tool not in ("Edit", "Write") or not isinstance(tool_input, dict):
        return None
    tool_input = {str(k): v for k, v in tool_input.items()}
    raw_path = str(tool_input.get("file_path", "")).replace("\\", "/")
    if not raw_path:
        return None
    path = Path(raw_path)
    root = repo_root(path)
    if root is None:
        return None
    rel = path.relative_to(root).as_posix()
    text = new_text(tool, tool_input)

    if rel.endswith(".py") and rel != STRIPE_HOME and STRIPE_IMPORT.search(text):
        return (
            "deny",
            f"Blocked: `import stripe` outside {STRIPE_HOME}. Providers are reached only "
            "through payments/port.py (CLAUDE.md: 'Providers are only reached through "
            "payments/port.py'). Add the capability to the PaymentProvider port, implement it "
            "in stripe_provider.py and fake.py, and call the port.",
        )

    is_migration = rel.startswith("migrations/versions/") and rel.endswith(".py")
    if is_migration and APP_IMPORT.search(text):
        return (
            "deny",
            "Blocked: migrations must not import app internals (`marketplace.*`). A "
            "migration has to replay identically after the models change; inline the "
            "column types (UTCDateTime renders as sa.DateTime(timezone=True)). See the "
            "migration skill.",
        )

    if rel.startswith("src/marketplace/templates/") and TEMPLATE_UNSAFE.search(text):
        return (
            "deny",
            "Blocked: `|safe` / `autoescape false` in a template. Jinja autoescape is the "
            "XSS boundary for seller-supplied content on unauthenticated pages (CLAUDE.md, "
            "tests/test_pages.py::test_xss_*). Render the value escaped.",
        )
    if rel.startswith("src/") and rel.endswith(".py") and PY_AUTOESCAPE_OFF.search(text):
        return (
            "deny",
            "Blocked: `autoescape=False`. Jinja autoescape is a security boundary here, "
            "not a formatting default (CLAUDE.md).",
        )

    if (
        is_migration
        and path.is_file()
        and os.environ.get("MARKETPLACE_ALLOW_MIGRATION_EDIT") != "1"
    ):
        ref = on_default_branch(root, rel)
        if ref is not None:
            current = path.read_text(encoding="utf-8")
            after = resulting_file(tool, tool_input, current)
            before_up, after_up = upgrade_source(current), upgrade_source(after)
            if before_up is None or before_up != after_up:
                return (
                    "ask",
                    f"{rel} is already on {ref}: databases have run its upgrade(). Changing "
                    "it forks the schema between old and new installs. Prefer a new revision. "
                    "Approve only if the old revision cannot run at all (e.g. fails on SQLite "
                    "from an empty DB) and the change leaves an already-migrated Postgres DB "
                    "identical (same constraint names).",
                )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        verdict = check({str(k): v for k, v in payload.items()})
        if verdict is not None:
            decide(*verdict)
    except Exception as exc:  # fail open: a broken guard must never brick a session
        print(f"guard_edit.py: internal error, allowing: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
