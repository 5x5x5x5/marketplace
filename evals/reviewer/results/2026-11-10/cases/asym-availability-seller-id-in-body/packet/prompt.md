Review the commit at HEAD in this repository (diff range `main...HEAD`, one commit).

Commit message:

> Let availability name the seller it is posted for
>
> Agencies run several seller accounts from one integration; accept an
> optional seller_id so they can post availability without juggling one
> token per seller.

The diff follows. Read the touched code in full in its files, as your instructions say, and report in your standard output format.

```diff
diff --git a/src/marketplace/models.py b/src/marketplace/models.py
index 0cafd06..3945412 100644
--- a/src/marketplace/models.py
+++ b/src/marketplace/models.py
@@ -664,6 +664,7 @@ class JobCreateRequest(BaseModel):
 
 class AvailabilityRequest(BaseModel):
     service_type_id: str = Field(min_length=1, max_length=128)
+    seller_id: str | None = Field(default=None, max_length=128)
 
 
 class ReviewRequest(BaseModel):
diff --git a/src/marketplace/services/profiles.py b/src/marketplace/services/profiles.py
index 24e3ba0..c1addf3 100644
--- a/src/marketplace/services/profiles.py
+++ b/src/marketplace/services/profiles.py
@@ -29,6 +29,7 @@ def get_profile(session: Session, seller_id: str) -> SellerProfile:
 
 
 def post_availability(session: Session, seller_id: str, req: AvailabilityRequest) -> None:
+    seller_id = req.seller_id or seller_id
     _require_active(session, seller_id)
     if session.get(ServiceType, req.service_type_id) is None:
         raise NotFoundError("unknown service_type_id")
```
