Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Show when a seller responded to an offer
>
> The seller's offer list includes accepted, declined and expired offers
> when filtered by status; responded_at lets the client show when each one
> closed.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/models.py b/src/marketplace/models.py
index 0cafd06..2caf506 100644
--- a/src/marketplace/models.py
+++ b/src/marketplace/models.py
@@ -206,6 +206,7 @@ class SellerOfferView(BaseModel):
     status: OfferStatus
     offered_at: datetime
     expires_at: datetime
+    responded_at: datetime | None
 
 
 class TransactionOut(BaseModel):
```
