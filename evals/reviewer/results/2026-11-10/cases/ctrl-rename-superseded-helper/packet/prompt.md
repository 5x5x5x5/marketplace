Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Rename _superseded to say what it checks
>
> The helper answers "has a newer event for this object already applied?";
> name it that way so the account_updated branch reads as its rule.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/webhooks.py b/src/marketplace/services/webhooks.py
index 7a5313a..e5d6826 100644
--- a/src/marketplace/services/webhooks.py
+++ b/src/marketplace/services/webhooks.py
@@ -61,7 +61,7 @@ def _applied_elsewhere(session: Session, event: PaymentEvent) -> bool:
     )
 
 
-def _superseded(session: Session, event: PaymentEvent) -> bool:
+def _newer_event_applied(session: Session, event: PaymentEvent) -> bool:
     """Has a NEWER event of this kind for this object (by arrival) already
     applied? A retried event can land long after later events for the same
     object; for last-value-wins state it must not overwrite them."""
@@ -149,7 +149,7 @@ def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
     elif event.kind == "account_updated":
         # Last value wins, by arrival: a retried older update must not
         # overwrite a newer one that already applied.
-        if _superseded(session, event):
+        if _newer_event_applied(session, event):
             return
         seller = session.scalar(
             select(SellerProfile).where(SellerProfile.provider_account_id == event.object_id)
```
