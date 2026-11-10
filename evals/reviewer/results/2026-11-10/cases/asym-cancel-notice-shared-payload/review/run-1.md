[Important] Cancel notices share one payload carrying both buyer_price and seller_payout
  where: <review-copy>/src/marketplace/services/jobs.py:110-119 (_notify_cancelled)
  rule: item 5 (notification payloads built from a shared dict that includes the other side's amount); CLAUDE.md: "seller payloads never carry buyer_price, buyer payloads never carry seller_payout"
  scenario: One `snapshot` dict with `buyer_price` and `seller_payout` is enqueued as the JOB_CANCELLED_SELLER payload (to the seller) and as the REFUND_ISSUED_BUYER payload (to the buyer). Both outbox rows now persist the other side's amount. The seller's row stores the buyer price, so the seller can work out the margin. The buyer's row stores the seller payout. The current renderers read only their own field, so no email leaks today. The `Notification.payload` is stored and handed whole to the renderers. `NotificationOut` does not expose it, so there is no direct API leak. Any new renderer, template change, MCP/admin dump of the payload, a log of the payload, or a send-failure `last_error` that echoes it would expose the other side's amount. The commit message says "each renderer picks the fields it needs", but the safety rule is on the payload, not on the renderer. This is a regression: before this commit each payload held only that role's amount.
  confidence: high
  fix: Build two role-specific payloads: seller gets job_id, service_type_id and seller_payout; buyer gets job_id and buyer_price. A shared helper may carry only the non-money keys.

[Important] No asymmetry test covers the cancel notification payloads
  where: <review-copy>/tests (test_notifications.py, test_notification_prefs.py)
  rule: item 9
  scenario: A grep of the notification tests finds no assertion that the JOB_CANCELLED_SELLER payload lacks buyer_price or that the REFUND_ISSUED_BUYER payload lacks seller_payout. The test at test_notifications.py:50 only checks the seller_payout field. This commit would therefore pass the existing suite.
  confidence: medium
  fix: Cancel a job with a seller and a refund, then assert on the stored Notification.payload rows that the forbidden key is absent for each role.

Checked: services/jobs.py _notify_cancelled; notifications.py (renderers 177-275, RENDERERS map, role map); models.py NotificationOut (no payload field); a grep of tests for the cancel kinds and the payload keys.
