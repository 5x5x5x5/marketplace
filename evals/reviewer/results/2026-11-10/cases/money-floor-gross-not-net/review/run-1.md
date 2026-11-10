[Critical] Inlined floor check drops the fee estimate, so matching uses the gross spread
  where: <review-copy>/src/marketplace/matching.py:84 (_priced)
  rule: item 8 and the CLAUDE.md non-negotiable "margin floor check is net-of-fees" (compare against required_spread(buyer_price, margin_floor, fees), never the gross spread)
  scenario: The default config has floor.absolute = 2.00 and fees = 2.9% + 0.30 on a 50.00 buyer price, so estimated_fee = 1.75. A candidate payout of 46.00 gives a spread of 4.00. The old code required 2.00 + 1.75 = 3.75, which 4.00 passes. Take a payout of 47.00 instead: the spread is 3.00, and 3.00 >= 2.00 holds, so the new code accepts it. The old code required 3.75 and rejected it. Quote and re-match (cheapest_payout, fifo and highest_rated all go through _priced) can now match a seller whose spread is positive but below fees plus floor. The Payment is charged and the platform nets less than the floor, or loses money. A later floor check in create_job (booking.py:188) uses passes_floor and rejects the same candidate. That check runs after matching, so the job breaks at booking time. The quote probe in booking.py:106-115 also uses required_spread, so it disagrees with matching and is no longer consistent with it.
  confidence: high
  fix: Revert to passes_floor(buyer_price, payout, cfg.margin_floor, cfg.fees), or inline buyer_price - payout >= required_spread(buyer_price, cfg.margin_floor, cfg.fees). Hoisting required_spread out of the loop is a valid speedup, because it depends only on buyer_price and cfg and not on the candidate.

[Important] No test pins the matching floor to the fee-inclusive threshold
  where: <review-copy>/src/marketplace/matching.py:77-86 (_priced)
  rule: item 10 (missing proof)
  scenario: The commit changes behavior and the diff contains no test change, so no existing test covers a spread between effective_floor and required_spread.
  confidence: medium (I did not search the tests directory)
  fix: Add a pure matching test with a spread inside that window, expecting None from each strategy.

Checked: the diff; matching.py in full (effective_floor, estimated_fee, required_spread, passes_floor, _priced, the three strategies); a grep of all callers of these helpers (booking.py:21, 106, 115 and 188). I did not read the tests.
