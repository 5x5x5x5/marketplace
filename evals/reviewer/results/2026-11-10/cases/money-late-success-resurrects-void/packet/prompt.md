Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Let a provider success settle the payment
>
> A success from the provider is authoritative: if the charge succeeded, the
> Payment row should say SUCCEEDED regardless of what we recorded earlier
> (decline-then-retry already relies on FAILED -> SUCCEEDED).

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/webhooks.py b/src/marketplace/services/webhooks.py
index 7a5313a..91ac9e9 100644
--- a/src/marketplace/services/webhooks.py
+++ b/src/marketplace/services/webhooks.py
@@ -129,18 +129,6 @@ def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
             return  # refunded is terminal — late events never resurrect the charge
         if event.kind == "payment_succeeded":
             job = session.get(Job, payment.job_id, with_for_update=True)
-            if payment.status is PaymentStatus.FAILED and (
-                job is None or job.status != JobStatus.AWAITING_PAYMENT
-            ):
-                # The charge was voided (cancel/sweep) and the job is dead — a late
-                # success must never resurrect it. Real providers don't emit success
-                # for a voided PI; races and replays can.
-                logger.warning(
-                    "ignoring late payment_succeeded for voided payment %s (job %s)",
-                    payment.id,
-                    payment.job_id,
-                )
-                return
             payment.status = PaymentStatus.SUCCEEDED
             if job is not None and job.status == JobStatus.AWAITING_PAYMENT:
                 job.status = JobStatus.ACCEPTED
```
