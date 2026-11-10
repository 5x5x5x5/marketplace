# Harness notes for packet r201b0e (do not show this file to the reviewer)

- Reviewer agent: `spec-compliance-reviewer` (fit: weak: no narrow reviewer lists this class; spec-compliance reviews it against the commit message and CLAUDE.md with no plan task)
- Make the working copy: `python3 evals/reviewer/run.py checkout ops-pagination-in-python /tmp/review-r201b0e`
- Run the reviewer with `/tmp/review-r201b0e` as its working directory and the exact contents of `prompt.md` as its task. Do not mention an evaluation, the case id, or this directory.
- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then `run-2.md`, ... for repeats), and grade it with `../grading.md`.
