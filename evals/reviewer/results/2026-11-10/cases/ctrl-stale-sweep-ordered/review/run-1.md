No findings.

The diff only adds `.order_by(Job.id)` to the unlocked stale-job select in `_sweep_stale_payments`. It does not change any lock, guard or provider call, and the new order is deterministic and unique. Two concurrent sweeps now walk the stale jobs in the same order, so they can't lock Payment and Job rows in opposite orders across jobs. The per-job order is still Payment, then Job with `populate_existing=True`, then a status re-check. The `PaymentStatus.SUCCEEDED` guard, the `cancel_charge` failure path (logged, then `continue`) and the `payment.status = FAILED` and `EXPIRED` transitions are untouched.

I did not trace the callers of `sweep` or the webhook applier, and I did not check whether the sweep has a Postgres race test.

Checked: <review-copy>/src/marketplace/services/maintenance.py lines 1-130 (`_sweep_stale_payments` in full, `sweep`, imports), against lock-order, stale-relock and idempotency rules.
