# Follow-up: the Postgres suite hung on this mutant

The mechanical run recorded `tests_pg: timeout` (the runner's 30-minute
limit). Run by hand on the same mutant against Postgres:

- The hang is `tests/test_disputes.py::test_concurrent_duplicate_resolve_cannot_double_book`.
  The test simulates a concurrent duplicate resolve by opening a second
  session inside the patched `reverse_transfer` leg and committing a
  RESOLVED dispute. Without the mid-handler `session.commit()`, the resolve
  request still holds the dispute row lock (`FOR UPDATE`) during the
  provider legs, so the second session waits on Postgres for that lock while
  the request waits on the second session in Python. The cycle runs through
  application code, so Postgres's deadlock detector never sees it and the
  test blocks forever. On SQLite the same test fails instead of hanging.
- The rest of the Postgres suite, with only that test deselected:
  `2 failed, 497 passed, 1 deselected`. Failing:
  `tests/test_disputes.py::test_resolve_pins_amounts_across_a_502_retry` and
  `tests/test_money_path_audit.py::test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry`
  (the same two the SQLite run caught, besides the concurrent-resolve test).

Reading: the Postgres layer does catch this mutant, but as a hung CI job
with no test named, not as a failure. The suite has no per-test timeout
(pytest-timeout is not a dependency), so a regression of this shape would
show up as a job killed by the CI time limit. The hang is also the mutant's
real-world effect in miniature: the dispute row stays locked across the
provider round trip, so a concurrent chargeback webhook for that dispute
would block for as long as the provider takes.
