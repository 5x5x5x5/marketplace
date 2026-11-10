Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Extract the shared cancel tail into one helper
>
> cancel_job and admin_cancel_job ended with the same eight lines (expire
> offers, release the payment, notify, mark cancelled). Move them into
> _cancel_locked, called with the Job already locked, so the two paths can't
> drift.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/jobs.py b/src/marketplace/services/jobs.py
index d77b263..1cd3fb2 100644
--- a/src/marketplace/services/jobs.py
+++ b/src/marketplace/services/jobs.py
@@ -93,6 +93,13 @@ def cancel_job(session: Session, buyer_id: str, job_id: UUID, provider: PaymentP
         raise NotFoundError("job not found")
     if job.status not in (JobStatus.PENDING, JobStatus.AWAITING_PAYMENT):
         raise ConflictError(f"cannot cancel a {job.status} job")
+    _cancel_locked(session, provider, job)
+    return job
+
+
+def _cancel_locked(session: Session, provider: PaymentProvider, job: Job) -> None:
+    """The tail both cancel paths share. The caller holds the Payment and Job
+    locks (in that order) and has re-checked the job's status under them."""
     _expire_open_offers(session, job.id)
     try:
         refunded = _release_payment(session, provider, job)
@@ -101,7 +108,6 @@ def cancel_job(session: Session, buyer_id: str, job_id: UUID, provider: PaymentP
         raise ProviderUnavailableError("payment provider unavailable, retry") from None
     _notify_cancelled(session, job, refunded)
     job.status = JobStatus.CANCELLED
-    return job
 
 
 def _notify_cancelled(session: Session, job: Job, refunded: bool) -> None:
@@ -368,13 +374,6 @@ def admin_cancel_job(
         raise NotFoundError("job not found")
     if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.EXPIRED):
         raise ConflictError(f"cannot cancel a {job.status} job")
-    _expire_open_offers(session, job.id)
-    try:
-        refunded = _release_payment(session, provider, job)
-    except PaymentError as exc:
-        logger.warning("payment release failed for job %s: %s", job.id, exc)
-        raise ProviderUnavailableError("payment provider unavailable, retry") from None
-    _notify_cancelled(session, job, refunded)
-    job.status = JobStatus.CANCELLED
+    _cancel_locked(session, provider, job)
     audit(session, admin_id, "cancel_job", str(job_id), {})
     return job
```
