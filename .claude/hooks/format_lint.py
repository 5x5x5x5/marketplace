#!/usr/bin/env python3
"""PostToolUse for Edit/Write on a .py file: ruff fix + format that one file.

Runs `ruff check --fix` then `ruff format` with the repo's pyproject config, so
the gate's `ruff format --check` never fails on whitespace. Remaining lint
findings are shown to Claude (exit 2 on PostToolUse = feedback; the edit has
already happened, nothing is blocked).

F401 (unused import) is neither fixed nor reported here. The format hook used
while this repo was built auto-removed "unused" imports, which deleted an
import added one edit before its first use; every implementation plan had to
carry a warning about it. The gate's `ruff check .` still catches an import
that stays unused.

`--force-exclude` keeps pyproject's `extend-exclude = ["migrations"]` in force
even though the file is named explicitly.

Fails open: no ruff, a timeout, or any error exits 0 silently. Stdlib only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def find_ruff(root: Path) -> list[str] | None:
    local = root / ".venv" / "bin" / "ruff"
    if local.is_file():
        return [str(local)]
    on_path = shutil.which("ruff")
    if on_path:
        return [on_path]
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        file_path = Path(str(tool_input.get("file_path", "")))
        if file_path.suffix != ".py" or not file_path.is_file():
            return 0
        root = next((p for p in file_path.parents if (p / "pyproject.toml").is_file()), None)
        if root is None:
            return 0
        ruff = find_ruff(root)
        if ruff is None:
            return 0
        common = ["--force-exclude", str(file_path)]
        subprocess.run(
            [*ruff, "check", "--fix", "--ignore", "F401", "--quiet", *common],
            cwd=root,
            capture_output=True,
            timeout=20,
            check=False,
        )
        subprocess.run(
            [*ruff, "format", "--quiet", *common],
            cwd=root,
            capture_output=True,
            timeout=20,
            check=False,
        )
        left = subprocess.run(
            [*ruff, "check", "--ignore", "F401", "--output-format", "concise", *common],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if left.returncode == 1 and left.stdout.strip():
            print(
                "ruff findings remain after auto-fix (fix them now; the gate will fail):\n"
                + left.stdout.strip(),
                file=sys.stderr,
            )
            return 2
    except Exception:  # fail open: formatting is a convenience, never a gate
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
