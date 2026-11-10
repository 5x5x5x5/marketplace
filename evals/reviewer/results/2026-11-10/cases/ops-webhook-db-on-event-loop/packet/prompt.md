Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Call the webhook processor directly
>
> The dedup insert and apply are a couple of indexed statements; the thread
> hop cost more than the work and made tracebacks harder to read.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/routers/payments.py b/src/marketplace/routers/payments.py
index bb5a8f3..a27a094 100644
--- a/src/marketplace/routers/payments.py
+++ b/src/marketplace/routers/payments.py
@@ -2,8 +2,6 @@
 signature checks stay here; dedup and apply run on a worker thread in
 `services.webhooks.process_webhook`, never on the event loop."""
 
-import asyncio
-
 from fastapi import APIRouter, HTTPException, Request
 
 from ..db import CommitRoute
@@ -31,4 +29,4 @@ async def payments_webhook(request: Request, provider: ProviderDep) -> dict[str,
         raise HTTPException(status_code=400, detail="invalid webhook signature") from None
     except (PaymentError, ValueError, KeyError):
         raise HTTPException(status_code=400, detail="malformed webhook payload") from None
-    return await asyncio.to_thread(_process_webhook, event)
+    return _process_webhook(event)
```
