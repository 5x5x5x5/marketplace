# Harness notes for packet r7e25b6 (do not show this file to the reviewer)

- Reviewer agent: `money-path-reviewer` (fit: in-scope)
- Make the working copy: `python3 evals/reviewer/run.py checkout money-a2-full-clawback-reads-as-failure /tmp/review-r7e25b6`
- Run the reviewer with `/tmp/review-r7e25b6` as its working directory and the exact contents of `prompt.md` as its task. Do not mention an evaluation, the case id, or this directory.
- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then `run-2.md`, ... for repeats), and grade it with `../grading.md`.
