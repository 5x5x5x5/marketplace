[Constraint] paginate now fetches every row and slices in Python
  where: <review-copy>/src/marketplace/services/common.py:21-29
  plan says: no plan file was supplied. Judged against /home/user/marketplace/CLAUDE.md, "List endpoints paginate in SQL": `paginate` takes a Select with a total order, and "Never fetch all rows and slice in Python."
  diff does: removes `.limit(limit).offset(offset)`. It runs `session.scalars(stmt).all()` and then `rows[offset : offset + limit]`. This is exactly the banned pattern.
  confidence: high
  fix: Revert. If the concern is large OFFSETs, use keyset pagination in a separate change. Don't load the whole table.

[Weakened-test] The SQL-pagination guard test fails with this change
  where: <review-copy>/tests/test_capacity.py:205 (`test_list_endpoints_paginate_in_sql`)
  plan says: each admin list endpoint's page read must contain "LIMIT" (`assert any("LIMIT" in s for s in page_reads)`).
  diff does: the statement no longer carries LIMIT, so every parametrized case that goes through `paginate` should fail. I did not run the suite, and the commit leaves the test unchanged. Either the gate was not run or the test is expected to break.
  confidence: medium
  fix: Revert the change, not the test.

[Extra] The commit message's rationale doesn't hold
  where: commit message
  plan says: n/a
  diff does: the message claims slicing keeps the statement shape the same and gives identical pages. Postgres already returns identical ordered pages with LIMIT/OFFSET. Slicing in Python makes memory and latency grow with table size on every page request, which is worse than the large-OFFSET cost it tries to avoid. The statement shape does not change with `offset` in the old code either.
  confidence: high
  fix: Drop the change.

[Scope] `catalog.visible_listings_page` still uses `.limit().offset()` (src/marketplace/catalog.py:105), so the change also leaves the two pagination paths inconsistent.
  confidence: medium

Checked: no plan file given, so I compared the diff to the CLAUDE.md invariants and the task description. I read src/marketplace/services/common.py, the `paginate` call sites (grep), tests/test_capacity.py:190-220, and catalog.py:105.
