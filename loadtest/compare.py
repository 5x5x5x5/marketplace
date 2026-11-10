"""Before/after Markdown table per stage from two result directories.

    python3 loadtest/compare.py loadtest/results/<before> loadtest/results/<after>

Reads each directory's `summary.json` (written by run_baseline.sh). Stages are
matched by label; a stage present in only one run is listed with a dash.
Stdlib only.
"""

import json
import sys
from pathlib import Path
from typing import Any

CONDITION_KEYS = ("git_commit", "workers", "catalog_size", "cpu_cores", "loadgen_shared_box")


def _load(path: Path) -> dict[str, Any]:
    file = path / "summary.json" if path.is_dir() else path
    return json.loads(file.read_text())


def _ms(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _pair(before: float | None, after: float | None, fmt: Any, *, higher_better: bool) -> str:
    left = fmt(before) if before is not None else "-"
    right = fmt(after) if after is not None else "-"
    if before and after is not None:
        change = (after - before) / before * 100
        better = (change > 0) == higher_better
        mark = "" if abs(change) < 1 else (" (better)" if better else " (worse)")
        return f"{left} -> {right} ({change:+.0f}%){mark}"
    return f"{left} -> {right}"


def compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    b = {s["label"]: s for s in before["stages"]}
    a = {s["label"]: s for s in after["stages"]}
    labels = list(b) + [label for label in a if label not in b]
    out = ["## Conditions", "", "| | before | after |", "|---|---|---|"]
    for key in CONDITION_KEYS:
        left, right = before["conditions"].get(key, "-"), after["conditions"].get(key, "-")
        out.append(f"| {key} | {left} | {right} |")
    differing = [
        k
        for k in (*CONDITION_KEYS, "env_overrides", "stage_seconds")
        if before["conditions"].get(k) != after["conditions"].get(k)
    ]
    if differing:
        out += ["", f"Conditions differ ({', '.join(differing)}): read the deltas accordingly."]
    out += [
        "",
        "## Stages",
        "",
        "| Stage | Users | req/s | p50 | p95 | Failure rate |",
        "|---|---:|---|---|---|---|",
    ]

    def get(s: dict[str, Any] | None, key: str) -> float | None:
        return None if s is None or key not in s else float(s[key])

    def rps(v: float) -> str:
        return f"{v:.1f}"

    def pct(v: float) -> str:
        return f"{100 * v:.1f}%"

    for label in labels:
        sb, sa = b.get(label), a.get(label)
        users = (sa or sb or {}).get("users", "-")
        cells = [
            _pair(get(sb, "rps"), get(sa, "rps"), rps, higher_better=True),
            _pair(get(sb, "p50_ms"), get(sa, "p50_ms"), _ms, higher_better=False),
            _pair(get(sb, "p95_ms"), get(sa, "p95_ms"), _ms, higher_better=False),
            _pair(get(sb, "failure_rate"), get(sa, "failure_rate"), pct, higher_better=False),
        ]
        out.append(f"| {label} | {users} | " + " | ".join(cells) + " |")
    out.append("")
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(compare(_load(Path(sys.argv[1])), _load(Path(sys.argv[2]))))


if __name__ == "__main__":
    main()
