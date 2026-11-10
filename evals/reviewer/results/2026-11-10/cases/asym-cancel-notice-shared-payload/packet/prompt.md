Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Build the cancel notices from one job snapshot
>
> Both notices described the same job with hand-copied keys; one snapshot
> keeps them consistent and each renderer picks the fields it needs.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/jobs.py b/src/marketplace/services/jobs.py
index d77b263..9d9cc78 100644
--- a/src/marketplace/services/jobs.py
+++ b/src/marketplace/services/jobs.py
@@ -107,24 +107,16 @@ def cancel_job(session: Session, buyer_id: str, job_id: UUID, provider: PaymentP
 def _notify_cancelled(session: Session, job: Job, refunded: bool) -> None:
     """Cancel notices: the committed seller always hears; the buyer only gets a
     receipt when money actually moved back (never a notice of their own action)."""
+    snapshot = {
+        "job_id": str(job.id),
+        "service_type_id": job.service_type_id,
+        "buyer_price": str(job.buyer_price),
+        "seller_payout": str(job.seller_payout),
+    }
     if job.seller_id is not None:
-        notifications.enqueue(
-            session,
-            EventKind.JOB_CANCELLED_SELLER,
-            job.seller_id,
-            {
-                "job_id": str(job.id),
-                "service_type_id": job.service_type_id,
-                "seller_payout": str(job.seller_payout),
-            },
-        )
+        notifications.enqueue(session, EventKind.JOB_CANCELLED_SELLER, job.seller_id, snapshot)
     if refunded:
-        notifications.enqueue(
-            session,
-            EventKind.REFUND_ISSUED_BUYER,
-            job.buyer_id,
-            {"job_id": str(job.id), "buyer_price": str(job.buyer_price)},
-        )
+        notifications.enqueue(session, EventKind.REFUND_ISSUED_BUYER, job.buyer_id, snapshot)
 
 
 def _release_payment(session: Session, provider: PaymentProvider, job: Job) -> bool:
```
