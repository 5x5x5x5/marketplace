[Important] Charge key now varies per offer, so a re-offered job can charge the buyer twice
  where: <review-copy>/src/marketplace/services/jobs.py:213 (accept_offer)
  rule: 4 (provider idempotency keys: a retry must replay the original operation's key), 5 (double effect), CLAUDE.md "Outbound idempotency keys ... derived from the job id (`charge:{job_id}`)"
  scenario:
    1. Seller A accepts offer O1 for job J. `provider.charge_buyer` runs with key `charge:O1`.
    2. The provider creates the PaymentIntent PI1 (or charges it, for Stripe), but the call times out or raises `PaymentError`, or the DB commit fails after the charge. `accept_offer` raises 502 and everything rolls back. The `Payment` row is not written, and O1 stays OFFERED.
    3. O1 expires, either lazily in `_sweep` or in `accept_offer` itself, and J is re-matched to seller B with offer O2.
    4. B accepts. The provider is called with key `charge:O2`, which is a new key, so it creates PI2. PI1 is never replayed.
    5. The buyer now has two PaymentIntents, and only PI2 is recorded in `Payment`. PI1 is orphaned: it can hold an uncancelled authorization or a real charge that nothing refunds.
    With the old key `charge:{job.id}` step 4 would have replayed PI1. The code comment above the call, "a retry gets the SAME PaymentIntent back — no strays", is now false across offers. The lock order and the guards are unchanged. `Payment.job_id` is unique, so only one of the two PIs is ever tracked.
  confidence: medium (the code path is traced in full; the provider-side double charge depends on how Stripe behaves when the call times out after creating the PI)
  fix: Revert to `charge:{job.id}`, since one job takes one buyer charge. If support needs the offer link, store `offer.id` in the PaymentIntent metadata or on the `Payment` row instead of in the key.

[Minor] No test covers a charge retried after the offer changes
  where: <review-copy>/src/marketplace/services/jobs.py:213 (accept_offer)
  rule: 10 (missing proof)
  scenario: The diff adds no test. A fake-provider test would show the problem: script a charge failure on O1's accept, expire O1, let B accept O2, then assert the provider holds a single PaymentIntent for the job. It would fail with this change.
  confidence: medium
  fix: Add that test alongside the key revert.

Checked: the full `accept_offer`, `_release_payment`, `decline_offer` and `complete_job` in `services/jobs.py`; a grep of every `idempotency_key` use under `src/`; the stale-payment sweep (it expires the job and never returns it to PENDING, so this path cannot re-offer); the `unique=True` constraint on `job_id` in `entities.py`.
