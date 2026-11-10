"""The seeded-bug cases in evals/reviewer/cases must keep applying to the
current tree. Each case is a list of exact text replacements; when code moves,
a case whose text no longer matches would silently stop measuring anything.
This fails first, so whoever moved the code re-anchors the case in the same
change. Fast by design: it reads files and never builds a scratch copy."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = sorted((ROOT / "evals" / "reviewer" / "cases").glob("*.toml"))

REQUIRED: dict[str, type] = {
    "id": str,
    "kind": str,
    "class": str,
    "origin": str,
    "synthetic": bool,
    "invariant": str,
    "location": str,
    "reviewer": str,
    "reviewer_fit": str,
    "removes_explanatory_comment": bool,
    "commit_message": str,
    "rubric": str,
    "edits": list,
}
REVIEWERS = {"money-path-reviewer", "asymmetry-reviewer", "spec-compliance-reviewer"}


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def test_there_are_bug_and_control_cases() -> None:
    kinds = [_load(p).get("kind") for p in CASES]
    assert kinds.count("bug") >= 12
    assert kinds.count("control") >= 3


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_has_required_metadata(path: Path) -> None:
    case = _load(path)
    for field, kind in REQUIRED.items():
        assert field in case, f"{path.name}: missing {field!r}"
        assert isinstance(case[field], kind), f"{path.name}: {field!r} must be {kind.__name__}"
    assert case["id"] == path.stem
    assert case["kind"] in ("bug", "control")
    assert case["reviewer"] in REVIEWERS
    for field in ("origin", "invariant", "location", "commit_message", "rubric"):
        assert str(case[field]).strip(), f"{path.name}: {field!r} is empty"
    bug = case["kind"] == "bug"
    for word in ("CAUGHT", "PARTIAL", "MISSED") if bug else ("CLEAN", "FALSE POSITIVE"):
        assert word in case["rubric"], f"{path.name}: rubric must define {word}"


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_mutation_applies_cleanly_and_uniquely(path: Path) -> None:
    """Edits apply in order; each `old` must occur exactly once in its file
    as it stands after the earlier edits (the runner's exact semantics)."""
    edits: list[dict[str, str]] = _load(path)["edits"]
    assert edits, f"{path.name}: no edits"
    contents: dict[str, str] = {}
    for i, edit in enumerate(edits):
        rel = edit["file"]
        assert not rel.startswith(("evals/", "tests/test_reviewer_eval_cases.py"))
        target = ROOT / rel
        assert target.is_file(), f"{path.name} edit {i}: {rel} does not exist"
        text = contents.setdefault(rel, target.read_text())
        assert edit["old"] != edit["new"], f"{path.name} edit {i}: old == new"
        count = text.count(edit["old"])
        assert count == 1, (
            f"{path.name} edit {i}: old text occurs {count} times in {rel}; "
            "re-anchor the case to the moved code (keep the same defect)"
        )
        contents[rel] = text.replace(edit["old"], edit["new"])
