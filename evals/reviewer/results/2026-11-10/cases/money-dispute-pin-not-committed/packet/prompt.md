Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Leave the dispute commit to CommitRoute
>
> resolve_dispute committed mid-call, which released the dispute lock before
> the provider legs ran and split one resolution across two transactions.
> Flush the pinned amounts instead and let the route commit once, atomically,
> on success.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/disputes.py b/src/marketplace/services/disputes.py
index 1b95a19..955f4cb 100644
--- a/src/marketplace/services/disputes.py
+++ b/src/marketplace/services/disputes.py
@@ -178,14 +178,11 @@ def resolve_dispute(
             # lying CLAWBACK row (fake provider) or 502 forever (real Stripe).
             raise ConflictError("no paid transfer to claw back")
 
-    # Pin the amounts now that every 4xx guard has passed, and commit: a
-    # provider leg below may still fail and 502, but the pin survives that
-    # rollback (get_session's except-block only rolls back work since the
-    # last commit), so a retry is forced to converge on these amounts instead
-    # of silently changing them after a leg may have already executed.
+    # Pin the amounts now that every 4xx guard has passed; a retry must
+    # converge on these amounts.
     dispute.refund_amount = refund_amount
     dispute.clawback_amount = clawback_amount
-    session.commit()
+    session.flush()
 
     # Provider legs, both idempotent by key: a failure raises 502 with nothing
     # further recorded, and the retry replays the succeeded leg (same key)
```
