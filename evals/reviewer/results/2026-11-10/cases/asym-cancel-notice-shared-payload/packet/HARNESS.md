# Harness notes for packet r8373cc (do not show this file to the reviewer)

- Reviewer agent: `asymmetry-reviewer` (fit: in-scope)
- Make the working copy: `python3 evals/reviewer/run.py checkout asym-cancel-notice-shared-payload /tmp/review-r8373cc`
- Run the reviewer with `/tmp/review-r8373cc` as its working directory and the exact contents of `prompt.md` as its task. Do not mention an evaluation, the case id, or this directory.
- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then `run-2.md`, ... for repeats), and grade it with `../grading.md`.
