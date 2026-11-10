Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Age out webhook ledger rows by time only
>
> The webhook ledger is a dedup window: once a row is older than the
> retention window the provider will not redeliver it, so its status doesn't
> matter for reaping. Simplifies the delete to one indexed range scan.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/maintenance.py b/src/marketplace/services/maintenance.py
index b35e7d4..125c648 100644
--- a/src/marketplace/services/maintenance.py
+++ b/src/marketplace/services/maintenance.py
@@ -27,7 +27,7 @@ from ..entities import (
     WebhookEvent,
 )
 from ..mail import get_mail_sender
-from ..models import EventKind, JobStatus, NotificationStatus, PaymentStatus, WebhookEventStatus
+from ..models import EventKind, JobStatus, NotificationStatus, PaymentStatus
 from ..payments import get_provider
 from ..payments.port import PaymentError, PaymentProvider
 from ..repo import audit
@@ -96,8 +96,7 @@ def _sweep_expired_auth(session: Session) -> None:
 
 def _sweep_retention(session: Session) -> None:
     """Bounded tables: reap replay/dedup rows and delivered mail past their
-    windows. PENDING outbox rows are immortal — the outbox contract holds —
-    and so are FAILED webhook rows: they are still owed a retry."""
+    windows. PENDING outbox rows are immortal — the outbox contract holds."""
     now = _now()
     session.execute(
         delete(IdempotencyRecord).where(
@@ -106,7 +105,6 @@ def _sweep_retention(session: Session) -> None:
     )
     session.execute(
         delete(WebhookEvent).where(
-            WebhookEvent.status != WebhookEventStatus.FAILED,
             WebhookEvent.received_at < now - timedelta(days=settings.retention_webhooks_days),
         )
     )
```
