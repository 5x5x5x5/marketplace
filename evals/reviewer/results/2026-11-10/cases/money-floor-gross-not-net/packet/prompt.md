Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Inline the floor check in _priced
>
> _priced runs once per candidate on every quote and re-match; compare the
> spread inline instead of going through two helper calls per candidate.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/matching.py b/src/marketplace/matching.py
index 02e29f6..014ea59 100644
--- a/src/marketplace/matching.py
+++ b/src/marketplace/matching.py
@@ -81,7 +81,7 @@ def _priced(
     out: list[tuple[Candidate, Decimal]] = []
     for c in candidates:
         payout = seller_payout_for(c, cfg, supply, demand)
-        if passes_floor(buyer_price, payout, cfg.margin_floor, cfg.fees):
+        if buyer_price - payout >= effective_floor(buyer_price, cfg.margin_floor):
             out.append((c, payout))
     return out
 
```
