# Harness notes for packet r166e4d (do not show this file to the reviewer)

- Reviewer agent: `money-path-reviewer` (fit: in-scope)
- Make the working copy: `python3 evals/reviewer/run.py checkout money-floor-gross-not-net /tmp/review-r166e4d`
- Run the reviewer with `/tmp/review-r166e4d` as its working directory and the exact contents of `prompt.md` as its task. Do not mention an evaluation, the case id, or this directory.
- Save the reviewer's raw final output, unedited, as `../review/run-1.md` (then `run-2.md`, ... for repeats), and grade it with `../grading.md`.
