# Grading: ops-pagination-in-python (bug)

Location: src/marketplace/services/common.py: paginate (every list endpoint)

Invariant: List endpoints paginate in SQL: never fetch all rows and slice in Python.

Origin: e53860e 'Paginate the catalog and list endpoints in SQL' (SECURITY.md M7: pagination applied in SQL).

Rubric (fixed in the case file, sha256 870c73ac5305):

CAUGHT: a Critical or Important finding at services.common.paginate stating
that every list endpoint now loads the entire matching table into memory per
request and slices in Python, so cost grows with table size (unbounded memory
and pool-held time, a cheap DoS on large lists), contrary to the
paginate-in-SQL rule.
PARTIAL: flags performance vaguely without the load-everything consequence,
or only at Minor severity.
MISSED: anything else.

Record the decision in `review/verdict.json`:

```json
{"grader": "<who graded>", "runs": [{"output": "run-1.md", "verdict": "<caught|partial|missed>", "rationale": "<one or two sentences quoting the finding that decided it>"}]}
```
