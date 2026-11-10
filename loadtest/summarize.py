"""Fold a run directory's Locust CSVs and stage metadata into summary.json/.md.

    python3 loadtest/summarize.py loadtest/results/<stamp>

Reads `conditions.json` and `stages.jsonl` (both written by run_baseline.sh)
plus each stage's `<label>_stats.csv`. Stdlib only.
"""

import csv
import json
import sys
from pathlib import Path
from typing import Any


def _num(row: dict[str, str], key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, ValueError):
        return 0.0


def _stats(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(aggregate, per-endpoint rows) from one Locust *_stats.csv."""
    aggregate: dict[str, Any] = {}
    endpoints: list[dict[str, Any]] = []
    if not path.exists():
        return aggregate, endpoints
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            requests = int(_num(row, "Request Count"))
            failures = int(_num(row, "Failure Count"))
            entry = {
                "name": row["Name"] if row["Name"] != "Aggregated" else "Aggregated",
                "method": row.get("Type", ""),
                "requests": requests,
                "failures": failures,
                "failure_rate": (failures / requests) if requests else 0.0,
                "rps": _num(row, "Requests/s"),
                "p50_ms": _num(row, "50%"),
                "p95_ms": _num(row, "95%"),
                "p99_ms": _num(row, "99%"),
                "avg_ms": _num(row, "Average Response Time"),
                "max_ms": _num(row, "Max Response Time"),
            }
            if row["Name"] == "Aggregated":
                aggregate = entry
            else:
                endpoints.append(entry)
    return aggregate, endpoints


def _job_delta(prev: dict[str, int], cur: dict[str, int]) -> dict[str, int]:
    keys = set(prev) | set(cur)
    return {k: cur.get(k, 0) - prev.get(k, 0) for k in sorted(keys)}


def summarize(results: Path) -> dict[str, Any]:
    conditions = json.loads((results / "conditions.json").read_text())
    stages: list[dict[str, Any]] = []
    prev_jobs: dict[str, int] = {}
    meta_path = results / "stages.jsonl"
    for line in meta_path.read_text().splitlines() if meta_path.exists() else []:
        meta = json.loads(line)
        aggregate, endpoints = _stats(results / f"{meta['label']}_stats.csv")
        jobs_total = {k.lower(): v for k, v in meta.get("jobs_total", {}).items()}
        stage: dict[str, Any] = {**meta, **aggregate, "endpoints": endpoints}
        stage.pop("jobs_total", None)
        stage["jobs_created_during_stage"] = _job_delta(prev_jobs, jobs_total)
        prev_jobs = jobs_total
        stages.append(stage)
    return {"conditions": conditions, "stages": stages}


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.0f} ms"


def markdown(summary: dict[str, Any]) -> str:
    cond = summary["conditions"]
    lines = ["# Load-test summary", "", "## Conditions", ""]
    lines += [
        f"- {k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in cond.items()
    ]
    lines += [
        "",
        "## Stages",
        "",
        "| Stage | Users | Secs | req/s | p50 | p95 | Failures | Completed jobs | Load avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summary["stages"]:
        completed = s.get("jobs_created_during_stage", {}).get("completed", 0)
        lines.append(
            f"| {s['label']} | {s['users']} | {s['seconds']} | {s.get('rps', 0):.1f} "
            f"| {_fmt_ms(s.get('p50_ms', 0))} | {_fmt_ms(s.get('p95_ms', 0))} "
            f"| {s.get('failures', 0)} / {s.get('requests', 0)} "
            f"({100 * s.get('failure_rate', 0):.1f}%) | {completed} "
            f"| {s.get('loadavg_before', '')} |"
        )
    lines += [
        "",
        "Completed jobs counts jobs reaching `completed` since the previous stage "
        "(cumulative table delta); it is only meaningful for lifecycle stages.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    results = Path(sys.argv[1])
    summary = summarize(results)
    (results / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (results / "summary.md").write_text(markdown(summary))
    print(markdown(summary))


if __name__ == "__main__":
    main()
