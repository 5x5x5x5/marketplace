Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Key the buyer charge by offer
>
> A job can be offered to several sellers in turn; keying the charge by the
> offer that was accepted ties the PaymentIntent to the acceptance that
> created it, which is what support looks up.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/jobs.py b/src/marketplace/services/jobs.py
index d77b263..41eb01c 100644
--- a/src/marketplace/services/jobs.py
+++ b/src/marketplace/services/jobs.py
@@ -210,7 +210,7 @@ def accept_offer(
             amount=job.buyer_price,
             currency=settings.currency,
             job_id=str(job.id),
-            idempotency_key=f"charge:{job.id}",
+            idempotency_key=f"charge:{offer.id}",
         )
     except PaymentError as exc:
         logger.warning("charge failed for job %s: %s", job.id, exc)
```
