[Critical] Commit moved into `get_session` teardown, which the repo forbids (F2 read-your-writes race)
  where: <review-copy>/src/marketplace/db.py:67 (get_session)
  rule: 6 (commit ordering) and the CLAUDE.md non-negotiable "never in get_session teardown"
  scenario: The locked fastapi is 0.136 (uv.lock). Since 0.118, FastAPI runs the exit code of `yield` dependencies after the response has been sent. Take a buyer's POST /jobs, cancel or complete. The server sends the 2xx. The buyer immediately sends a chained GET or POST on a new session. That session can run before `session.commit()` executes and read pre-write state, for example a job still PENDING or a payment still unsettled. A chained write can then be guarded against stale state and book a second effect. This is the bug `CommitRoute` was written to prevent. `tests/test_live_server.py` is the regression test for it, and it is the test most likely to fail. I did not run it.
  confidence: medium (I did not run it; the 0.118+ exit timing is from my knowledge of FastAPI, not traced in the installed package)
  fix: Revert to committing in `CommitRoute` before the response is sent. If the session-dependency design is kept, the commit must run before the send, for example in the route handler wrapper.

[Critical] A commit failure after the response is sent turns a 2xx into silent loss
  where: db.py:67 (get_session)
  rule: 6 and 5 (the client sees success for money state that did not persist)
  scenario: The commit now runs after the 2xx is on the wire. If it raises (serialization failure, deadlock, connection loss), the client has already been told the charge, refund or completion succeeded. The idempotency middleware buffers and stores that 2xx for replay, so a retry with the same key replays the success. The provider call already happened, so there is a refund or transfer with no DB row. Previously `CommitRoute` committed before the response was released, so a commit failure became a 500.
  confidence: medium
  fix: Same as above: commit before the response is released.

[Important] Handlers that return a 4xx normally, rather than raising, now commit their writes
  where: db.py:67 with <review-copy>/src/marketplace/pages.py:195-213 (signup_submit, login_submit, _not_found)
  rule: 6 (state on an error response) and the CLAUDE.md note that "CommitRoute only commits 2xx"
  scenario: `signup_submit` catches `HTTPException` from `auth.signup` and returns a 400 template. `login_submit` returns a 401 template. Before, `status_code < 400` meant nothing was committed. Now the dependency sees a normal return and commits whatever the session holds. The one concrete case: `auth.signup` can raise a 409 or `HTTPException` after `db.add(user)` and flush. Those flushed writes are then committed even though the user was shown an error. A future handler that returns a 4xx `Response` after partial writes will also persist them.
  confidence: medium (the commit-on-normal-return behavior is traced; I did not find a page path that writes money rows before returning an error)
  fix: Commit only when `response.status_code < 400`, or roll back explicitly on the page error paths.

[Important] `CommitRoute` is now a no-op, and the guard tests assert the old design
  where: db.py:36-56 (CommitRoute); <review-copy>/tests/test_architecture.py:877 (test_get_session_teardown_does_not_commit)
  rule: 10 (missing proof) and the CLAUDE.md "statically checkable rules are checked"
  scenario: `test_get_session_teardown_does_not_commit` asserts `get_session` contains no `.commit`, so this commit should fail the gate. The diff does not touch that test. The `CommitRoute` marker also no longer protects anything: a router without it now commits anyway, so the startup check guards nothing. The CLAUDE.md invariants and the docstring at the top of db.py still describe the old design.
  confidence: high
  fix: Revert the commit. If the design change is intended, change the invariant, test and CLAUDE.md deliberately and keep a live-server ordering test.

Checked: db.get_session and CommitRoute before and after the diff; the uv.lock fastapi version (0.136); pages.py signup, login and `_not_found` error returns; auth.signup and login; the test_architecture commit-in-get_session guard; test_live_server's purpose.
