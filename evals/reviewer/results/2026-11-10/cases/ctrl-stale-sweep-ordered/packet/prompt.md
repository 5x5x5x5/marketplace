Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Sweep stale payments in a fixed order
>
> Concurrent sweeps lock each stale job's Payment and Job in the order the
> SELECT returns them; an explicit order keeps that order identical across
> runners.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/maintenance.py b/src/marketplace/services/maintenance.py
index b35e7d4..1087af6 100644
--- a/src/marketplace/services/maintenance.py
+++ b/src/marketplace/services/maintenance.py
@@ -43,7 +43,9 @@ def _sweep_stale_payments(session: Session, provider: PaymentProvider) -> None:
     """Jobs stuck AWAITING_PAYMENT past the TTL expire and free the seller's slot."""
     deadline = _now() - timedelta(minutes=settings.payment_ttl_minutes)
     stale = session.scalars(
-        select(Job).where(Job.status == JobStatus.AWAITING_PAYMENT, Job.accepted_at < deadline)
+        select(Job)
+        .where(Job.status == JobStatus.AWAITING_PAYMENT, Job.accepted_at < deadline)
+        .order_by(Job.id)
     ).all()
     for job in stale:
         # Re-lock (payment first, then job — same order as _apply_payment_event)
```
