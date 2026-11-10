Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Drop the dispute lookup from the transfer_failed branch
>
> The payout row should mirror the provider's view of the transfer; the
> extra query on every transfer event special-cased one path and made the
> branch harder to reason about.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/services/webhooks.py b/src/marketplace/services/webhooks.py
index 7a5313a..12cb96f 100644
--- a/src/marketplace/services/webhooks.py
+++ b/src/marketplace/services/webhooks.py
@@ -165,23 +165,7 @@ def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
                 # A failed/reversed transfer is terminal for that transfer id,
                 # so applying it late is truthful; notify only on the
                 # transition so a replay doesn't re-page admins.
-                # A dispute clawback of the whole payout fully reverses the
-                # transfer, which the provider reports exactly like a failure.
-                # The seller was paid and then clawed back: FAILED would invite
-                # an admin payout retry that pays them again. The clawback pin
-                # is committed before the reversal leg runs, so it is always
-                # visible here, and it is a permanent row (replay-safe).
-                clawback = session.scalar(
-                    select(Dispute.clawback_amount).where(Dispute.job_id == payout.job_id)
-                )
-                if clawback is not None and clawback >= payout.amount:
-                    logger.info(
-                        "transfer %s fully reversed by dispute clawback; payout %s stays %s",
-                        event.object_id,
-                        payout.id,
-                        payout.status,
-                    )
-                elif payout.status is not PayoutStatus.FAILED:
+                if payout.status is not PayoutStatus.FAILED:
                     payout.status = PayoutStatus.FAILED
                     notifications.enqueue_admins(
                         session,
```
