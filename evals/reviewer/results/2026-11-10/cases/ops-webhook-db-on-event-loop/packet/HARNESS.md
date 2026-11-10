# Harness notes for packet r74c6ef (do not show this file to the reviewer)

- Reviewer agent: `money-path-reviewer` (fit: in-scope (item 7: DB work on the coroutine))
- Make the working copy: `python3 evals/reviewer/run.py checkout ops-webhook-db-on-event-loop /tmp/review-r74c6ef`
- Run the reviewer with `/tmp/review-r74c6ef` as its working directory and the exact contents of `prompt.md` as its task. Do not mention an evaluation, the case id, or this directory.
- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then `run-2.md`, ... for repeats), and grade it with `../grading.md`.
