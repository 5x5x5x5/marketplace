No findings.

The commit is a pure extraction. `cancel_job` and `admin_cancel_job` both still take the Payment lock, then the Job lock with `populate_existing=True`, then re-check status, and only then call `_cancel_locked`. The moved lines run in the same order as before: expire offers, release payment (a `PaymentError` becomes a 502 `ProviderUnavailableError` with a log line), notify, set CANCELLED.

- **Admin audit row:** `audit()` still runs after `_cancel_locked` in `admin_cancel_job`, so a failed cancel never writes an audit row.
- **Refund key:** the `refund:{job.id}` key inside `_release_payment` is unchanged.
- **Status guards:** the two guards still differ on purpose. The buyer path allows PENDING and AWAITING_PAYMENT. The admin path also allows ACCEPTED.
- **Missing proof:** I did not check the test suite, so I can't say whether the existing cancel-vs-complete race tests cover both paths.

Checked: `<review-copy>/src/marketplace/services/jobs.py` — `cancel_job`, `admin_cancel_job`, `_cancel_locked`, `_release_payment`, `_notify_cancelled`. Lock order, stale re-lock, provider key reuse, and double effect all traced on both paths.
