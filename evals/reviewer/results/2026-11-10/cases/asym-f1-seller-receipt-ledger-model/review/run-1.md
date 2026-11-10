[Critical] Seller completion receipt returns admin TransactionOut (buyer price and margin leak to every seller)
  where: <review-copy>/src/marketplace/routers/seller.py:146 (POST /v1/seller/jobs/{job_id}/complete, complete_job)
  rule: item 1 (wrong view family on a route); CLAUDE.md asymmetry non-negotiable
  scenario: Any seller completes a job and receives {job_id, buyer_price, seller_payout, margin, completed_at}. `TransactionOut` (models.py:211) declares `buyer_price` and `margin`, and its docstring says it must never be returned from a buyer or seller route. This is the same leak recorded in docs/tryout-findings-2026-07-16.md (F1). The commit removes the `SellerCompletionOut` view (job_id, seller_payout, completed_at), which was the fix for that leak.
  confidence: high
  fix: Revert to `response_model=SellerCompletionOut` and restore the import. The "near-duplicate" view is the redaction, so keep it.

[Important] Existing asymmetry test will fail, so the change cannot pass the gate as is
  where: <review-copy>/tests/test_information_asymmetry.py:67 (test_completion_receipt_is_seller_scoped)
  rule: item 9 (proof)
  scenario: The test asserts `buyer_price` and `margin` are absent and that the keys are exactly {job_id, seller_payout, completed_at}. It is the only guard on this endpoint. I did not run it. I expect it to fail against this diff, and it should not be loosened to get the commit through.
  confidence: high
  fix: Revert the change. Leave the test as it is.

Checked: routers/seller.py complete_job and its imports, models.py TransactionOut and SellerCompletionOut, routers/admin.py:218 (the only legitimate TransactionOut user), tests/test_information_asymmetry.py receipt test, docs/tryout-findings-2026-07-16.md. I did not open any templates. The diff has no enqueue sites.
