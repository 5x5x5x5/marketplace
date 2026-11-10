No findings.

The commit is a pure rename of a private helper. A repo-wide grep shows no remaining `_superseded` reference. The only two occurrences of `_newer_event_applied` are the definition and its single call site in the `account_updated` branch. The body, signature and call arguments are unchanged, so there is no change to locking, idempotency keys, commit ordering, webhook dedup or ledger behavior. I did not read the helper body beyond the diff context, because the rename alters no logic.

Checked: grep for old and new helper names across the repo (<review-copy>/src/marketplace/services/webhooks.py:64 and :152, the definition and the `account_updated` branch of `_apply_payment_event`).
