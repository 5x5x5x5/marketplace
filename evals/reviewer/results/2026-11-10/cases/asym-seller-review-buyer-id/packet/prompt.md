Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Include the reviewed buyer in the seller review receipt
>
> Mirrors ReviewOut, which carries the reviewed seller's id, so clients can
> render both receipts with one component.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/models.py b/src/marketplace/models.py
index 0cafd06..774f107 100644
--- a/src/marketplace/models.py
+++ b/src/marketplace/models.py
@@ -340,15 +340,13 @@ class ReviewOut(BaseModel):
 
 
 class SellerReviewOut(BaseModel):
-    """Seller's view of their own review of a buyer. `buyer_id` withheld —
-    every other seller-facing view withholds buyer identity too (asymmetry
-    doctrine); the seller already knows the job, and echoing it back would be
-    the first surface that lets a seller correlate buyers across jobs."""
+    """Seller's view of their own review of a buyer."""
 
     model_config = ConfigDict(from_attributes=True)
 
     id: UUID
     job_id: UUID
+    buyer_id: str
     rating: int
     comment: str | None = Field(default=None, validation_alias="public_comment")
     created_at: datetime
```
