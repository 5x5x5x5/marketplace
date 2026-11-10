Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Expire every open offer when a job is cancelled
>
> SKIP LOCKED could leave an offer OFFERED on a cancelled job until the TTL
> sweep caught it, and sellers kept seeing it in their offer list. Wait for
> the row instead so a cancel always closes all of the job's offers.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/offers.py b/src/marketplace/services/offers.py
index 0f04a60..c97c796 100644
--- a/src/marketplace/services/offers.py
+++ b/src/marketplace/services/offers.py
@@ -85,15 +85,12 @@ def match_and_offer(session: Session, job: Job) -> None:
 
 
 def expire_open_offers(session: Session, job_id: UUID) -> None:
-    # Callers already hold the Job lock, and accept/decline/the offer sweep
-    # lock Offer -> Job: waiting here for an offer one of them holds is a
-    # deadlock. SKIP LOCKED leaves that offer to its holder, which finds the
-    # job no longer PENDING once we commit (accept 409s, decline and the
-    # sweep do not re-match).
+    # Every open offer on a cancelled job must end EXPIRED; skipping a locked
+    # one would leave it OFFERED until its TTL.
     for offer in session.scalars(
         select(Offer)
         .where(Offer.job_id == job_id, Offer.status == OfferStatus.OFFERED)
-        .with_for_update(skip_locked=True)
+        .with_for_update()
     ).all():
         offer.status = OfferStatus.EXPIRED
         offer.responded_at = _now()
```
