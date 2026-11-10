"""Seeded-bug evaluation of this repo's layers of defense.

Every case under `cases/` re-introduces one known defect (or, for a control,
a correct change) as exact text replacements. For each case this script
builds a clean scratch copy of the repo at HEAD, applies and commits the
mutation there, and records what the mechanical layers say: the test suite on
SQLite (and Postgres with --pg-url), `tests/test_architecture.py` alone, and
the Semgrep rules. It also writes a review packet per case for the reviewer
agents, which a harness outside this script runs. A second mode assembles the
saved reviewer outputs and grading verdicts into the results table.

Standard library only. Run from anywhere; paths resolve from this file.

  python3 evals/reviewer/run.py check
  python3 evals/reviewer/run.py mechanical [--pg-url URL] [--jobs N] [--cases ID ...]
  python3 evals/reviewer/run.py checkout CASE_ID DEST
  python3 evals/reviewer/run.py assemble [--results DIR]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CASES_DIR = HERE / "cases"
RESULTS_DIR = HERE / "results"

# Paths left out of every scratch copy: the cases and rubrics themselves (a
# reviewer must not be able to read the answer) and the guard test, which
# fails by design once a mutation is applied.
EXCLUDED = ("evals/", "tests/test_reviewer_eval_cases.py")

SEMGREP = ["uvx", "semgrep==1.184.0"]
REQUIRED = (
    "id",
    "kind",
    "class",
    "origin",
    "synthetic",
    "invariant",
    "location",
    "reviewer",
    "reviewer_fit",
    "removes_explanatory_comment",
    "commit_message",
    "rubric",
    "edits",
)
KINDS = ("bug", "control")
REVIEWERS = ("money-path-reviewer", "asymmetry-reviewer", "spec-compliance-reviewer")
BUG_VERDICTS = ("caught", "partial", "missed")
CONTROL_VERDICTS = ("clean", "false_positive", "control_defect")
NEUTRAL_GIT = {
    "GIT_AUTHOR_NAME": "contributor",
    "GIT_AUTHOR_EMAIL": "contributor@example.invalid",
    "GIT_COMMITTER_NAME": "contributor",
    "GIT_COMMITTER_EMAIL": "contributor@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


# ---------- cases ----------


def load_case(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        case = tomllib.load(fh)
    case["_path"] = path
    case["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return case


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    cases = [load_case(p) for p in sorted(CASES_DIR.glob("*.toml"))]
    if only:
        unknown = set(only) - {c["id"] for c in cases}
        if unknown:
            sys.exit(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in only]
    return cases


def validate(case: dict[str, Any]) -> list[str]:
    errors = [f"missing field {f!r}" for f in REQUIRED if f not in case]
    if errors:
        return errors
    if case["id"] != case["_path"].stem:
        errors.append(f"id {case['id']!r} does not match file name")
    if case["kind"] not in KINDS:
        errors.append(f"kind must be one of {KINDS}")
    if case["reviewer"] not in REVIEWERS:
        errors.append(f"reviewer must be one of {REVIEWERS}")
    for field in ("commit_message", "rubric", "invariant", "location", "origin"):
        if not str(case[field]).strip():
            errors.append(f"{field} is empty")
    if not case["edits"]:
        errors.append("no edits")
    for i, edit in enumerate(case["edits"]):
        for key in ("file", "old", "new"):
            if key not in edit:
                errors.append(f"edit {i}: missing {key!r}")
        if edit.get("old") == edit.get("new"):
            errors.append(f"edit {i}: old == new")
        if str(edit.get("file", "")).startswith(EXCLUDED):
            errors.append(f"edit {i}: targets an excluded path")
    return errors


def apply_edits(root: Path, case: dict[str, Any], write: bool) -> list[str]:
    """Apply each edit in order; each `old` must occur exactly once in its
    file as it stands after the previous edits. Returns problems (empty when
    the whole mutation applies). Nothing is written unless `write`."""
    contents: dict[str, str] = {}
    problems: list[str] = []
    for i, edit in enumerate(case["edits"]):
        rel = edit["file"]
        if rel not in contents:
            target = root / rel
            if not target.is_file():
                problems.append(f"edit {i}: {rel} does not exist")
                continue
            contents[rel] = target.read_text()
        count = contents[rel].count(edit["old"])
        if count != 1:
            problems.append(f"edit {i}: old text occurs {count} times in {rel} (need exactly 1)")
            continue
        contents[rel] = contents[rel].replace(edit["old"], edit["new"])
    if write and not problems:
        for rel, text in contents.items():
            (root / rel).write_text(text)
    return problems


# ---------- scratch copies ----------


def run(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def child_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "DATABASE_URL", "PYTHONPATH")
    }
    env.update(extra)
    return env


def head_sha() -> str:
    return run(["git", "rev-parse", "HEAD"], REPO).stdout.strip()


def export_head(dest: Path) -> None:
    """HEAD's tree (never the working tree) minus the excluded paths, as a
    fresh git repo with one neutral base commit and no history to read."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"], cwd=REPO, capture_output=True, check=True
    )
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        members = [m for m in tar.getmembers() if not m.name.startswith(EXCLUDED)]
        tar.extractall(dest, members=members, filter="data")
    env = child_env(**NEUTRAL_GIT)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "Base"],
    ):
        result = run(cmd, dest, env)
        if result.returncode != 0:
            raise RuntimeError(f"{cmd}: {result.stderr}")


def make_copy(case: dict[str, Any] | None, dest: Path) -> None:
    export_head(dest)
    if case is None:
        return
    problems = apply_edits(dest, case, write=True)
    if problems:
        raise RuntimeError(f"{case['id']}: mutation does not apply: {problems}")
    env = child_env(**NEUTRAL_GIT)
    # On a branch, so both `main...HEAD` and `HEAD~1..HEAD` name the change.
    run(["git", "checkout", "-q", "-b", "change"], dest, env)
    run(["git", "add", "-A"], dest, env)
    result = run(["git", "commit", "-q", "-m", case["commit_message"].strip()], dest, env)
    if result.returncode != 0:
        raise RuntimeError(f"commit failed: {result.stderr}")


# ---------- mechanical layers ----------


def _nodeid(classname: str, name: str, root: Path) -> str:
    if not classname:
        return name
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = Path(*parts[:cut]).with_suffix(".py")
        if (root / candidate).is_file():
            return "::".join([candidate.as_posix(), *parts[cut:], name])
    return f"{classname}::{name}"


def parse_junit(path: Path, root: Path) -> tuple[dict[str, str], int]:
    """Failed or errored test ids -> first line of the message; plus the
    number of test cases reported."""
    if not path.is_file():
        return {}, 0
    failed: dict[str, str] = {}
    total = 0
    for case in ET.parse(path).getroot().iter("testcase"):
        total += 1
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                nodeid = _nodeid(case.get("classname", ""), case.get("name", ""), root)
                message = (node.get("message") or node.text or tag).strip().splitlines()
                failed[nodeid] = (message[0] if message else tag)[:300]
    return failed, total


def run_pytest(
    root: Path, env: dict[str, str], args: list[str], label: str, timeout: float
) -> dict[str, Any]:
    junit = root / f".eval-{label}.xml"
    started = time.monotonic()
    try:
        result = run(
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *args],
            root,
            env,
            timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "seconds": round(time.monotonic() - started)}
    failed, total = parse_junit(junit, root)
    tail = result.stdout.strip().splitlines()[-1:] if result.stdout.strip() else []
    return {
        "status": "pass" if result.returncode == 0 else "fail",
        "exit_code": result.returncode,
        "tests_reported": total,
        "failed": failed,
        "summary": tail[0] if tail else result.stderr.strip()[-300:],
        "seconds": round(time.monotonic() - started),
    }


def confirm_failures(
    root: Path, env: dict[str, str], failed: dict[str, str], label: str
) -> list[str]:
    """Re-run the failing tests once; ids that fail again are 'confirmed'.
    Collection errors have no runnable id and are kept as confirmed."""
    runnable = [n for n in failed if "::" in n]
    confirmed = [n for n in failed if "::" not in n]
    if runnable:
        again = run_pytest(root, env, runnable, f"{label}-rerun", 900)
        confirmed += [n for n in runnable if n in again.get("failed", {})]
    return sorted(confirmed)


def run_semgrep(root: Path) -> dict[str, Any]:
    try:
        result = run(
            [*SEMGREP, "--config", ".semgrep/rules", "--metrics=off", "--json", "src", "scripts"],
            root,
            child_env(),
            600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "not run", "reason": str(exc)[:300]}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "not run", "reason": (result.stderr or "no JSON output").strip()[-300:]}
    findings = sorted(
        f"{r['check_id'].rsplit('.', 1)[-1]} {r['path']}:{r['start']['line']}"
        for r in data.get("results", [])
    )
    errors = [str(e.get("message", ""))[:200] for e in data.get("errors", [])]
    return {"status": "ran", "findings": findings, "errors": errors}


def run_static(root: Path, env: dict[str, str]) -> dict[str, Any]:
    """Plausibility check, not a measured layer: a seeded diff should pass the
    lint/format/type gate the way a submitted change would."""
    out: dict[str, Any] = {}
    for name, cmd in (
        ("ruff_check", ["uv", "run", "ruff", "check", "."]),
        ("ruff_format", ["uv", "run", "ruff", "format", "--check", "."]),
        ("pyright", ["uv", "run", "pyright"]),
    ):
        result = run(cmd, root, env, 600)
        out[name] = "pass" if result.returncode == 0 else "fail"
        if result.returncode != 0:
            out[f"{name}_output"] = (result.stdout + result.stderr).strip()[-1500:]
    return out


def ensure_database(root: Path, url: str) -> None:
    """Create the Postgres database named in `url` if it is missing, using
    the scratch copy's own SQLAlchemy (this script stays stdlib-only)."""
    code = (
        "import os\n"
        "from sqlalchemy import create_engine, text\n"
        "from sqlalchemy.engine import make_url\n"
        "u = make_url(os.environ['EVAL_DB_URL'])\n"
        "e = create_engine(u.set(database='postgres'), isolation_level='AUTOCOMMIT')\n"
        "with e.connect() as c:\n"
        "    q = text('SELECT 1 FROM pg_database WHERE datname = :d')\n"
        "    if c.execute(q, {'d': u.database}).scalar() is None:\n"
        "        c.execute(text(f'CREATE DATABASE \"{u.database}\"'))\n"
    )
    result = run(["uv", "run", "python", "-c", code], root, child_env(EVAL_DB_URL=url), 120)
    if result.returncode != 0:
        raise RuntimeError(f"could not create database: {result.stderr[-500:]}")


def slot_url(base: str, slot: int, jobs: int) -> str:
    if jobs == 1:
        return base
    head, _, db = base.rpartition("/")
    return f"{head}/{db}_s{slot}"


def mechanical_for(
    case: dict[str, Any] | None, pg_url: str | None, skip_static: bool, workdir: Path
) -> dict[str, Any]:
    label = case["id"] if case else "_baseline"
    root = workdir / label
    if root.exists():
        shutil.rmtree(root)
    make_copy(case, root)
    env = child_env()
    sync = run(["uv", "sync", "--frozen", "-q"], root, env, 900)
    if sync.returncode != 0:
        raise RuntimeError(f"{label}: uv sync failed: {sync.stderr[-500:]}")
    record: dict[str, Any] = {"case": label}
    if case is not None:
        diff = run(["git", "diff", "HEAD~1", "HEAD"], root).stdout
        record["diff"] = diff
    record["static"] = {"status": "skipped"} if skip_static else run_static(root, env)
    record["architecture"] = run_pytest(root, env, ["tests/test_architecture.py"], "arch", 600)
    sqlite = run_pytest(root, env, [], "sqlite", 1800)
    if sqlite.get("failed"):
        sqlite["confirmed"] = confirm_failures(root, env, sqlite["failed"], "sqlite")
    record["tests_sqlite"] = sqlite
    if pg_url:
        ensure_database(root, pg_url)
        pg_env = child_env(DATABASE_URL=pg_url)
        pg = run_pytest(root, pg_env, [], "pg", 1800)
        if pg.get("failed"):
            pg["confirmed"] = confirm_failures(root, pg_env, pg["failed"], "pg")
        record["tests_pg"] = pg
    else:
        record["tests_pg"] = {"status": "not run", "reason": "no --pg-url given"}
    record["semgrep"] = run_semgrep(root)
    shutil.rmtree(root, ignore_errors=True)
    return record


# ---------- packets ----------


def packet_code(case_id: str) -> str:
    """Opaque, stable label for a case, so nothing the reviewer sees (working
    copy name, prompt) carries the case id, which names the bug."""
    return "r" + hashlib.sha256(case_id.encode()).hexdigest()[:6]


def write_packet(case: dict[str, Any], diff: str, case_dir: Path) -> None:
    packet = case_dir / "packet"
    packet.mkdir(parents=True, exist_ok=True)
    code = packet_code(case["id"])
    message = case["commit_message"].strip()
    (packet / "diff.patch").write_text(diff)
    (packet / "commit-message.txt").write_text(message + "\n")
    quoted = "\n".join(f"> {line}" if line else ">" for line in message.splitlines())
    (packet / "prompt.md").write_text(
        "Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).\n\n"
        f"Commit message:\n\n{quoted}\n\n"
        "The diff follows. Read the touched code in full in its files, as your "
        "instructions say, and report in your standard output format.\n\n"
        f"```diff\n{diff}```\n"
    )
    (packet / "HARNESS.md").write_text(
        f"# Harness notes for packet {code} (do not show this file to the reviewer)\n\n"
        f"- Reviewer agent: `{case['reviewer']}` (fit: {case['reviewer_fit']})\n"
        f"- Make the working copy: `python3 evals/reviewer/run.py checkout {case['id']} "
        f"/tmp/review-{code}`\n"
        f"- Run the reviewer with `/tmp/review-{code}` as its working directory and the "
        "exact contents of `prompt.md` as its task. Do not mention an evaluation, the "
        "case id, or this directory.\n"
        "- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then "
        "`run-2.md`, ... for repeats), and grade it with `../grading.md`.\n"
    )
    grading = case_dir / "grading.md"
    grading.write_text(
        f"# Grading: {case['id']} ({case['kind']})\n\n"
        f"Location: {case['location']}\n\n"
        f"Invariant: {case['invariant']}\n\n"
        f"Origin: {case['origin']}\n\n"
        f"Rubric (fixed in the case file, sha256 {case['_sha256'][:12]}):\n\n"
        f"{case['rubric'].strip()}\n\n"
        "Record the decision in `review/verdict.json`:\n\n"
        "```json\n"
        '{"grader": "<who graded>", "runs": [{"output": "run-1.md", '
        f'"verdict": "<{"|".join(BUG_VERDICTS if case["kind"] == "bug" else CONTROL_VERDICTS)}>", '
        '"rationale": "<one or two sentences quoting the finding that decided it>"}]}\n'
        "```\n"
    )


def checkout(case_id: str, dest: Path) -> None:
    (case,) = load_cases([case_id])
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"{dest} exists and is not empty")
    make_copy(case, dest)
    print(f"review copy ready at {dest} (mutation committed at HEAD)")


# ---------- grading and the table ----------


def _new(case_layer: dict[str, Any], base_layer: dict[str, Any], key: str) -> list[str]:
    return sorted(set(case_layer.get(key) or []) - set(base_layer.get(key) or []))


def layer_outcomes(mech: dict[str, Any], base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each measured layer -> {"outcome": flagged|silent|not run|..., detail}.
    Test layers exclude tests/test_architecture.py, which is its own layer."""
    out: dict[str, dict[str, Any]] = {}
    for layer in ("tests_sqlite", "tests_pg"):
        cur = mech.get(layer, {})
        if cur.get("status") in ("not run", None):
            out[layer] = {"outcome": "not run"}
            continue
        if cur.get("status") == "timeout":
            out[layer] = {"outcome": "flagged", "detail": ["suite timed out"]}
            continue
        base_failed = set((base.get(layer) or {}).get("failed") or {})
        new = sorted(
            n
            for n in (cur.get("failed") or {})
            if n not in base_failed and not n.startswith("tests/test_architecture.py")
        )
        confirmed = set(cur.get("confirmed") or [])
        out[layer] = {
            "outcome": "flagged" if new else "silent",
            "detail": new,
            "unconfirmed_on_rerun": sorted(n for n in new if n not in confirmed),
        }
    arch = mech.get("architecture", {})
    base_arch = set((base.get("architecture") or {}).get("failed") or {})
    new_arch = sorted(n for n in (arch.get("failed") or {}) if n not in base_arch)
    if arch.get("status") == "timeout":
        new_arch = ["timed out"]
    out["architecture"] = {"outcome": "flagged" if new_arch else "silent", "detail": new_arch}
    sem = mech.get("semgrep", {})
    if sem.get("status") != "ran":
        out["semgrep"] = {"outcome": "not run", "detail": [sem.get("reason", "")]}
    else:
        new_sem = _new(sem, base.get("semgrep") or {}, "findings")
        out["semgrep"] = {"outcome": "flagged" if new_sem else "silent", "detail": new_sem}
    return out


def reviewer_outcome(case: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    verdict_path = case_dir / "review" / "verdict.json"
    if not verdict_path.is_file():
        return {"outcome": "pending", "runs": []}
    data = json.loads(verdict_path.read_text())
    allowed = BUG_VERDICTS if case["kind"] == "bug" else CONTROL_VERDICTS
    runs: list[dict[str, Any]] = []
    for entry in data.get("runs", []):
        verdict = entry.get("verdict")
        if verdict not in allowed:
            sys.exit(f"{verdict_path}: verdict {verdict!r} not in {allowed}")
        output = case_dir / "review" / str(entry.get("output", ""))
        if not output.is_file():
            sys.exit(f"{verdict_path}: raw reviewer output {output.name!r} is missing")
        runs.append({**entry, "output": f"review/{output.name}"})
    if not runs:
        return {"outcome": "pending", "runs": []}
    counts = {v: sum(r["verdict"] == v for r in runs) for v in allowed}
    if case["kind"] == "bug":
        majority = max(BUG_VERDICTS, key=lambda v: (counts[v], -BUG_VERDICTS.index(v)))
    else:
        majority = "false_positive" if counts["false_positive"] else "clean"
    return {
        "outcome": majority,
        "counts": {k: v for k, v in counts.items() if v},
        "runs": runs,
        "grader": data.get("grader"),
    }


def _fmt(cell: dict[str, Any], kind: str) -> str:
    outcome = cell["outcome"]
    if outcome in ("flagged", "silent"):
        if kind == "bug":
            return "**caught**" if outcome == "flagged" else "missed"
        return "**false positive**" if outcome == "flagged" else "clean"
    if outcome == "pending" or outcome == "not run":
        return outcome
    counts = cell.get("counts") or {}
    total = sum(counts.values())
    shown = outcome.replace("_", " ")
    return f"**{shown}** ({counts.get(outcome, 0)}/{total})" if total > 1 else f"**{shown}**"


def assemble(results: Path) -> None:
    cases = {c["id"]: c for c in load_cases()}
    base_path = results / "baseline.json"
    base = json.loads(base_path.read_text()) if base_path.is_file() else {}
    rows: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        case_dir = results / "cases" / case_id
        mech_path = case_dir / "mechanical.json"
        if not mech_path.is_file():
            continue
        mech = json.loads(mech_path.read_text())
        layers = layer_outcomes(mech, base)
        layers["reviewer"] = reviewer_outcome(case, case_dir)
        rows.append(
            {
                "id": case_id,
                "kind": case["kind"],
                "class": case["class"],
                "origin": case["origin"],
                "synthetic": case["synthetic"],
                "location": case["location"],
                "reviewer": case["reviewer"],
                "reviewer_fit": case["reviewer_fit"],
                "packet": f"cases/{case_id}/packet/prompt.md",
                "packet_code": packet_code(case_id),
                "case_changed_since_run": mech.get("case_sha256") != case["_sha256"],
                "static_gate": mech.get("static", {}),
                "layers": layers,
            }
        )
    summary = summarize(rows)
    meta_path = results / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    (results / "results.json").write_text(
        json.dumps({"run": meta, "summary": summary, "cases": rows}, indent=2) + "\n"
    )
    (results / "results.md").write_text(render_md(meta, summary, rows, base))
    print(f"wrote {results / 'results.md'} and results.json")


MECH_LAYERS = ("tests", "architecture", "semgrep")


def _caught(row: dict[str, Any], layer: str) -> bool | None:
    """True/False for a decided layer, None while undecided (not run, pending)."""
    layers = row["layers"]
    if layer == "tests":
        cells = [layers["tests_sqlite"], layers["tests_pg"]]
        if any(c["outcome"] == "flagged" for c in cells):
            return True
        return False if any(c["outcome"] == "silent" for c in cells) else None
    if layer == "reviewer":
        outcome = layers["reviewer"]["outcome"]
        return None if outcome == "pending" else outcome == "caught"
    outcome = layers[layer]["outcome"]
    return None if outcome == "not run" else outcome == "flagged"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bugs = [r for r in rows if r["kind"] == "bug"]
    controls = [r for r in rows if r["kind"] == "control"]
    layers = (*MECH_LAYERS, "reviewer")
    per_layer: dict[str, Any] = {}
    for layer in layers:
        decided = [r for r in bugs if _caught(r, layer) is not None]
        per_layer[layer] = {
            "caught": sum(bool(_caught(r, layer)) for r in decided),
            "decided": len(decided),
            "of": len(bugs),
        }
    for layer in ("tests_sqlite", "tests_pg"):
        decided = [r for r in bugs if r["layers"][layer]["outcome"] != "not run"]
        per_layer[layer] = {
            "caught": sum(r["layers"][layer]["outcome"] == "flagged" for r in decided),
            "decided": len(decided),
            "of": len(bugs),
        }
    reviewer_decided = all(_caught(r, "reviewer") is not None for r in bugs)
    counted = layers if reviewer_decided else MECH_LAYERS

    def catchers(row: dict[str, Any]) -> list[str]:
        return [layer for layer in counted if _caught(row, layer)]

    exactly_one = {r["id"]: catchers(r)[0] for r in bugs if len(catchers(r)) == 1}
    none = [r["id"] for r in bugs if not catchers(r)]
    partial = [r["id"] for r in bugs if r["layers"]["reviewer"]["outcome"] == "partial"]
    fps: dict[str, list[str]] = {}
    for row in controls:
        hit = [
            layer
            for layer in ("tests_sqlite", "tests_pg", "architecture", "semgrep")
            if row["layers"][layer]["outcome"] == "flagged"
        ]
        if row["layers"]["reviewer"]["outcome"] == "false_positive":
            hit.append("reviewer")
        if hit:
            fps[row["id"]] = hit
    return {
        "bug_cases": len(bugs),
        "control_cases": len(controls),
        "per_layer": per_layer,
        "layers_counted_for_overlap": list(counted),
        "reviewer_complete": reviewer_decided,
        "caught_by_exactly_one_layer": exactly_one,
        "caught_by_none": none,
        "reviewer_partial": partial,
        "control_false_positives": fps,
    }


def render_md(
    meta: dict[str, Any],
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    base: dict[str, Any],
) -> str:
    lines = [
        f"# Seeded-bug evaluation: results {meta.get('date', '')}",
        "",
        f"Repo commit: `{meta.get('head', '?')}`. Postgres: "
        f"{'yes' if meta.get('pg') else 'not run'}. Semgrep: {meta.get('semgrep', '?')}.",
        "",
        "Test columns exclude `tests/test_architecture.py`, which is its own column. "
        "A test layer counts as catching a case when at least one test that passes on the "
        "unmodified tree fails on the mutant. The reviewer column is graded against the "
        "rubric in each case file; raw reviewer output sits next to each verdict. "
        "See `evals/README.md` for what this does and does not show.",
        "",
        "## Summary",
        "",
        "| Layer | Bugs caught |",
        "|---|---|",
    ]
    names = {
        "tests_sqlite": "Tests on SQLite",
        "tests_pg": "Tests on Postgres",
        "tests": "Tests (either backend)",
        "architecture": "Architecture tests",
        "semgrep": "Semgrep rules",
        "reviewer": "Reviewer agent (caught; partial not counted)",
    }
    for key in ("tests_sqlite", "tests_pg", "tests", "architecture", "semgrep", "reviewer"):
        cell = summary["per_layer"][key]
        if cell["decided"] == 0:
            value = "pending" if key == "reviewer" else "not run"
        else:
            value = f"{cell['caught']} / {cell['decided']}"
        lines.append(f"| {names[key]} | {value} |")
    counted = ", ".join(summary["layers_counted_for_overlap"])
    one = summary["caught_by_exactly_one_layer"]
    lines += [
        "",
        f"Overlap below counts these layers: {counted}"
        + ("" if summary["reviewer_complete"] else " (reviewer still pending)")
        + ".",
        "",
        "- Caught by exactly one layer: "
        + (", ".join(f"`{k}` ({v})" for k, v in one.items()) or "none"),
        "- Caught by no layer: "
        + (", ".join(f"`{c}`" for c in summary["caught_by_none"]) or "none"),
        "- Reviewer partial catches: "
        + (", ".join(f"`{c}`" for c in summary["reviewer_partial"]) or "none"),
        "- False positives on controls: "
        + (
            ", ".join(
                f"`{k}` ({', '.join(v)})" for k, v in summary["control_false_positives"].items()
            )
            or "none"
        ),
        "",
        "## Cases",
        "",
        "| Case | Kind | Class | Tests SQLite | Tests PG | Arch | Semgrep | Reviewer |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        layers = row["layers"]
        cells = [
            _fmt(layers[k], row["kind"])
            for k in ("tests_sqlite", "tests_pg", "architecture", "semgrep", "reviewer")
        ]
        synthetic = " (synthetic)" if row["synthetic"] and row["kind"] == "bug" else ""
        lines.append(
            f"| `{row['id']}` | {row['kind']}{synthetic} | {row['class']} | "
            + " | ".join(cells)
            + " |"
        )
    lines += ["", "## Detail", ""]
    for row in rows:
        layers = row["layers"]
        lines += [f"### `{row['id']}`", "", f"- Origin: {row['origin']}"]
        lines.append(f"- Where: {row['location']}")
        lines.append(
            f"- Reviewer: `{row['reviewer']}` ({row['reviewer_fit']}); packet "
            f"`{row['packet']}` (code {row['packet_code']})"
        )
        static = row["static_gate"]
        bad = [k for k in ("ruff_check", "ruff_format", "pyright") if static.get(k) == "fail"]
        if bad:
            lines.append(f"- Static gate on the mutant FAILED: {', '.join(bad)}")
        for key, label in (
            ("tests_sqlite", "SQLite"),
            ("tests_pg", "Postgres"),
            ("architecture", "Architecture"),
            ("semgrep", "Semgrep"),
        ):
            cell = layers[key]
            detail = cell.get("detail") or []
            if cell["outcome"] == "flagged" and detail:
                shown = ", ".join(f"`{d}`" for d in detail[:8])
                more = f" and {len(detail) - 8} more" if len(detail) > 8 else ""
                lines.append(f"- {label}: {shown}{more}")
                flaky = cell.get("unconfirmed_on_rerun") or []
                if flaky:
                    shown_flaky = ", ".join(f"`{f}`" for f in flaky)
                    lines.append(f"  - did not fail again on rerun: {shown_flaky}")
            elif cell["outcome"] == "not run":
                lines.append(f"- {label}: not run")
            else:
                lines.append(f"- {label}: nothing")
        rev = layers["reviewer"]
        if rev["runs"]:
            for r in rev["runs"]:
                lines.append(f"- Reviewer {r['output']}: {r['verdict']}: {r.get('rationale', '')}")
        else:
            lines.append("- Reviewer: pending")
        if row["case_changed_since_run"]:
            lines.append("- WARNING: the case file changed after the mechanical run")
        lines.append("")
    if base:
        lines += [
            "## Baseline (unmodified tree)",
            "",
            f"- SQLite: {base.get('tests_sqlite', {}).get('summary', 'n/a')}",
            f"- Postgres: {base.get('tests_pg', {}).get('summary', 'n/a')}",
            f"- Architecture: {base.get('architecture', {}).get('summary', 'n/a')}",
            f"- Semgrep findings: {len(base.get('semgrep', {}).get('findings') or [])}",
            "",
        ]
    return "\n".join(lines)


# ---------- commands ----------


def cmd_check() -> int:
    bad = 0
    for case in load_cases():
        problems = validate(case) or apply_edits(REPO, case, write=False)
        status = "ok" if not problems else "; ".join(problems)
        bad += bool(problems)
        print(f"{case['id']}: {status}")
    return 1 if bad else 0


def cmd_mechanical(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    for case in cases:
        problems = validate(case) or apply_edits(REPO, case, write=False)
        if problems:
            sys.exit(f"{case['id']}: {problems}")
    date = args.date or dt.date.today().isoformat()
    results = Path(args.results) if args.results else RESULTS_DIR / date
    results.mkdir(parents=True, exist_ok=True)
    jobs = max(1, args.jobs)
    slots: queue.Queue[int] = queue.Queue()
    for i in range(jobs):
        slots.put(i)
    workdir = Path(tempfile.mkdtemp(prefix="reviewer-eval-"))

    def work(case: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        slot = slots.get()
        try:
            url = slot_url(args.pg_url, slot, jobs) if args.pg_url else None
            label = case["id"] if case else "_baseline"
            print(f"[start] {label} (slot {slot})", flush=True)
            started = time.monotonic()
            record = mechanical_for(case, url, args.skip_static, workdir)
            record["seconds"] = round(time.monotonic() - started)
            if case is not None:
                record["case_sha256"] = case["_sha256"]
            print(f"[done]  {label} in {record['seconds']}s", flush=True)
            return label, record
        finally:
            slots.put(slot)

    targets: list[dict[str, Any] | None] = [None, *cases] if not args.no_baseline else [*cases]
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(work, t): t for t in targets}
        for future in concurrent.futures.as_completed(futures):
            target = futures[future]
            label = target["id"] if target else "_baseline"
            try:
                _, record = future.result()
            except Exception as exc:  # one broken case must not lose the others
                failures.append(f"{label}: {exc}")
                print(f"[error] {label}: {exc}", flush=True)
                continue
            if target is None:
                (results / "baseline.json").write_text(json.dumps(record, indent=2) + "\n")
                continue
            case_dir = results / "cases" / label
            case_dir.mkdir(parents=True, exist_ok=True)
            write_packet(target, record.pop("diff"), case_dir)
            (case_dir / "mechanical.json").write_text(json.dumps(record, indent=2) + "\n")
    shutil.rmtree(workdir, ignore_errors=True)
    run_meta_path = results / "run.json"
    meta = json.loads(run_meta_path.read_text()) if run_meta_path.is_file() else {}
    meta.update(
        {
            "date": date,
            "head": head_sha(),
            "pg": bool(args.pg_url) or meta.get("pg", False),
            "semgrep": " ".join(SEMGREP) + " --config .semgrep/rules src scripts",
        }
    )
    run_meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    assemble(results)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    return 1 if failures else 0


def latest_results() -> Path:
    dirs = sorted(p for p in RESULTS_DIR.iterdir() if p.is_dir())
    if not dirs:
        sys.exit("no results directory yet; run `mechanical` first")
    return dirs[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate case files against the working tree (no writes)")
    mech = sub.add_parser("mechanical", help="run the mechanical layers, write packets and table")
    mech.add_argument("--pg-url", help="Postgres URL (with --jobs N: databases <name>_s0..)")
    mech.add_argument("--jobs", type=int, default=1)
    mech.add_argument("--cases", nargs="*", help="case ids (default: all)")
    mech.add_argument("--date", help="results directory name (default: today)")
    mech.add_argument("--results", help="explicit results directory")
    mech.add_argument("--skip-static", action="store_true", help="skip the ruff/pyright check")
    mech.add_argument("--no-baseline", action="store_true", help="reuse an existing baseline.json")
    co = sub.add_parser("checkout", help="make a review copy with one case's mutation committed")
    co.add_argument("case_id")
    co.add_argument("dest", type=Path)
    asm = sub.add_parser("assemble", help="fold reviewer verdicts into results.md/json")
    asm.add_argument("--results", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        return cmd_check()
    if args.command == "mechanical":
        return cmd_mechanical(args)
    if args.command == "checkout":
        checkout(args.case_id, args.dest)
        return 0
    assemble(args.results or latest_results())
    return 0


if __name__ == "__main__":
    sys.exit(main())
