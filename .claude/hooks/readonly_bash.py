#!/usr/bin/env python3
"""PreToolUse on Bash for the reviewer subagents: read-only commands only.

Reviewers report findings; they never edit, commit, migrate, or start
servers. Their `tools` list already omits Edit/Write; this closes the Bash
side by allowing a short list of inspection commands and denying the rest.

Allowed: git diff/log/show/status/blame/grep/ls-files/rev-parse/merge-base/
cat-file/branch --list; rg, grep, ls, cat, head, tail, wc, sort, uniq, cut,
diff, pwd, echo; sed -n (no -i); find without -exec/-delete/-ok/-fprint;
uv run alembic heads/history/show. Pipes and && between allowed commands are
fine. Output redirection (other than to /dev/null), command substitution,
and backticks are denied because they can hide a write.

Fails open on its own errors (exit 0, no decision): the subagent then falls
back to the session's normal permission flow, which still prompts for writes.
"""

from __future__ import annotations

import json
import shlex
import sys

SIMPLE = {
    "rg", "grep", "ls", "cat", "head", "tail", "wc", "sort", "uniq", "cut",
    "diff", "pwd", "echo", "true",
}  # fmt: skip
GIT_READ = {
    "diff", "log", "show", "status", "blame", "grep", "ls-files", "rev-parse",
    "merge-base", "cat-file", "shortlog", "describe",
}  # fmt: skip
FIND_WRITE = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
SEPARATORS = {"|", "||", "&&", ";", "&", "|&"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}


def tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def segments(toks: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for tok in toks:
        if tok in SEPARATORS:
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def writes_somewhere(toks: list[str]) -> bool:
    """A redirect whose target is not /dev/null or another fd (2>&1)."""
    for i, tok in enumerate(toks):
        if tok in REDIRECTS:
            target = toks[i + 1] if i + 1 < len(toks) else ""
            if tok == ">&" and target in {"1", "2"}:
                continue
            if target != "/dev/null":
                return True
    return False


def allowed(words: list[str]) -> bool:
    if not words:
        return True
    cmd, args = words[0], words[1:]
    if cmd in SIMPLE:
        return True
    if cmd == "sed":
        return "-n" in args and not any(a.startswith("-i") or a == "--in-place" for a in args)
    if cmd == "find":
        return not FIND_WRITE.intersection(args)
    if cmd == "git":
        # `git -c key=val` can make git run an arbitrary program (core.pager,
        # diff.external), so it is never allowed; `-C dir` just changes dir.
        rest = list(args)
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag == "-c" or flag.startswith("--config"):
                return False
            if flag == "-C" and rest:
                rest.pop(0)
        sub, sub_args = (rest[0], rest[1:]) if rest else ("", [])
        if sub == "branch":
            return sub_args in ([], ["--list"], ["-a"], ["-r"])
        return sub in GIT_READ and not any(
            a.startswith(("--output", "--ext-diff")) for a in sub_args
        )
    if words[:3] == ["uv", "run", "alembic"]:
        return len(words) > 3 and words[3] in {"heads", "history", "show"}
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = str((payload.get("tool_input") or {}).get("command", ""))
        reason = None
        if "$(" in command or "`" in command or "<(" in command:
            reason = "command substitution is not allowed for reviewers"
        else:
            toks = tokens(command)
            if writes_somewhere(toks):
                reason = "output redirection is not allowed for reviewers"
            else:
                bad = [seg for seg in segments(toks) if not allowed(seg)]
                if bad:
                    reason = f"not a read-only inspection command: {' '.join(bad[0])!r}"
        if reason:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"Reviewer subagents are read-only: {reason}. Use git diff/log/"
                                "show, rg/grep, cat/sed -n, and report findings instead of "
                                "changing anything."
                            ),
                        }
                    }
                )
            )
    except Exception as exc:  # fail open: the normal permission flow still applies
        print(f"readonly_bash.py: internal error, no decision: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
