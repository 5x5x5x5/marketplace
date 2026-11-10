[Critical] Availability body `seller_id` overrides the authenticated seller (impersonation)
  where: src/marketplace/models.py:667 (AvailabilityRequest.seller_id); src/marketplace/services/profiles.py:32 (post_availability, `seller_id = req.seller_id or seller_id`); route src/marketplace/routers/seller.py:59 (POST availability)
  rule: item 4 / CLAUDE.md non-negotiable "Identity comes from the authenticated principal, never a request body. Never add a buyer_id/seller_id body field."
  scenario: Any authenticated seller (or any holder of a seller token) POSTs availability with {"service_type_id": "...", "seller_id": "<victim>"}. The service replaces the principal's id with the body value. It then calls `repo.get_or_create_seller` for that id and inserts an `Availability` row for the victim. The caller can switch on dispatch for services the victim never chose, which routes jobs and offers to the victim. The caller can also create a SellerProfile for an arbitrary id. `_require_active` runs against the victim's id, not the caller's. So a suspended caller can act through an active seller's id, and the response differs for an unknown id, a suspended id, and an active id. That lets the caller enumerate which seller ids exist and are active, which breaks the ownership-check consistency the neighbouring endpoints keep (item 6). No check ties the caller to the target seller. The "agency with several accounts" use case has no agency-to-seller relationship in the model to authorize it.
  confidence: high
  fix: Remove `seller_id` from AvailabilityRequest and the override line in post_availability. If agencies need multi-seller posting, build an explicit, authorized agency-to-seller link and derive the target from the principal.

[Important] No test for the changed surface
  where: tests (no asymmetry or ownership test covers POST seller availability with a foreign seller_id)
  rule: item 9
  scenario: The commit adds no test asserting that a body `seller_id` cannot redirect the write. This is how the override ships unnoticed.
  confidence: medium
  fix: Add a test that posting availability with another seller's id leaves that seller's availability untouched (or the field is rejected or ignored).

Checked: the diff; src/marketplace/models.py AvailabilityRequest; src/marketplace/services/profiles.py (post_availability, delete_availability); the grep of post_availability/AvailabilityRequest usages (routers/seller.py:59 passes the principal `SellerId` into the service). The route's response model and the other templates, notifications and enqueue sites are not touched by the diff.
