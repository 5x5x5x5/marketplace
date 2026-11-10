[Critical] Seller review receipt now returns buyer_id to the seller
  where: <review-copy>/src/marketplace/models.py:342-349 (SellerReviewOut); used by POST /v1/seller/jobs/{job_id}/review in routers/seller.py:109
  rule: item 2 (forbidden fields in a role's model: seller-facing buyer_id); CLAUDE.md asymmetry
  scenario: Any seller who reviews a completed job gets `buyer_id` (the buyer's user id) in the 201 response body. The deleted docstring said this was withheld on purpose: it would be the first seller-facing surface that lets a seller correlate buyers across jobs. Other seller views (SellerJobView and the seller offer view) carry no buyer_id. A seller can now collect buyer ids across jobs and recognise the same buyer.
  confidence: high
  fix: Revert the change and restore the docstring. If a client needs a shared component, make the buyer_id field optional on the client side. Do not add it to the seller model.

[Important] Existing asymmetry test now fails, and the diff touches no tests
  where: tests/test_seller_reviews.py:84 (`assert "buyer_id" not in body`)
  rule: item 9 (missing proof)
  scenario: The diff leaves the test that guards this field unchanged, so the suite fails or the guard gets weakened later. The test also checks only top-level keys, not a recursive scan.
  confidence: high
  fix: Keep the assertion and revert the model change. Optionally make the key scan recursive.

Checked: models.py SellerReviewOut and the other buyer_id-bearing models (QuoteOut, BuyerJobView, and the model at line 458); routers/seller.py review route (line 109); tests/test_seller_reviews.py. I did not check enqueue sites or templates. The diff touches none.
