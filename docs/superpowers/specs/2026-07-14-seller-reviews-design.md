# Seller→Buyer Reviews (+ carried minors) — Design

**Date:** 2026-07-14
**Status:** Approved
**Scope:** Trust & safety sub-phase 2 of 4 (disputes ✓ → **seller→buyer reviews** → moderation/abuse → notification preferences), plus the four minors explicitly carried from the disputes branch.

## Goal

Close the review loop: sellers rate buyers after a completed job, mirroring the
existing buyer→seller review exactly. The buyer aggregate is **display-only** —
it gates nothing (no matching changes, no offer surfacing, no thresholds).
Danny's explicit calls: display-only; independent reviews (no double-blind —
retaliation has no teeth while ratings gate nothing; revisit when they do).

## Decisions (Danny, 2026-07-14)

1. **Effect of a buyer rating:** display only. Aggregate visible to the buyer
   (own profile) and admins. Not shown to sellers pre-accept; does not touch
   matching.
2. **Retaliation handling:** none — independent reviews, no reveal mechanics.
3. **Shape:** mirror table (`seller_reviews`), NOT a generalized bidirectional
   `reviews` table (rewriting a working feature + data migration for zero
   current benefit) and NOT counters-only (loses comment/audit and needs a
   one-per-job marker anyway).
4. **Riders:** this branch also carries the four minors parked by the disputes
   final review (see §Riders).

## Data model

New entity `SellerReview` (table `seller_reviews`), a field-for-field mirror of
`Review`:

| column       | type                     | notes                       |
|--------------|--------------------------|-----------------------------|
| `id`         | UUID PK                  | default uuid4               |
| `job_id`     | FK jobs.id, **unique**   | one review per job          |
| `seller_id`  | String(128)              | author                      |
| `buyer_id`   | String(128), **indexed** | subject (aggregate lookups) |
| `rating`     | int                      | 1–5, schema-validated       |
| `comment`    | String(2000) nullable    |                             |
| `created_at` | tz-aware timestamp       | default now                 |

`BuyerProfile` gains `rating_count`/`rating_sum` (int, default 0) and the same
`rating` property `SellerProfile` has (`sum/count`, `None` when count is 0).

**Migration #6** (sixth from scratch; current head is #5 disputes):
`seller_reviews` table, the two `buyer_profiles` columns, **and the rider**:
`CheckConstraint("amount >= 0")` on `adjustments.amount` (the entity docstring
already promises "amounts are positive; kind carries the sign" — the DB now
enforces what the app asserts; named constraint so Alembic renders it
portably on both backends).

## API

### `POST /v1/seller/jobs/{job_id}/review` → `SellerReviewOut`

Guard ladder mirrors `review_job` (`api.py:457`) exactly, same order, same
codes:

1. Job missing **or** `job.seller_id != seller_id` → **404** (existence hidden
   from non-parties, consistent with the rest of the seller router).
2. `job.status is not JobStatus.COMPLETED` → **409** "can only review a
   completed job".
3. Existing `SellerReview` for the job → **409** "job already reviewed".
4. Insert row; fetch the profile via `repo.get_or_create_buyer` (the
   codebase's existing idiom — a completed job implies the profile exists,
   but don't assume); `rating_count += 1`, `rating_sum += body.rating`;
   return the row.

Request schema `SellerReviewRequest` ≡ `ReviewRequest` (rating `ge=1 le=5`,
comment `max_length=2000`). Response `SellerReviewOut` ≡ `ReviewOut` with
`buyer_id` in place of `seller_id`.

No review window (the buyer→seller review has none — stay symmetric). No
notification (parked for the notification-preferences sub-phase). Concurrency:
the `job_id` UNIQUE constraint is the backstop; a concurrent duplicate must
return the same 409 as the sequential path, not 500 (see rider (a) — same
IntegrityError-catch pattern, applied here from day one).

### Display surfaces (both new — no buyer-profile read endpoint exists today)

- `GET /v1/buyer/me` → `BuyerProfileOut` (`id`, `rating`, `rating_count`,
  `completed_jobs`). The buyer sees their own aggregate; individual seller
  reviews/comments are **not** exposed to the buyer (display-only aggregate;
  comment visibility is a moderation-phase question).
- `GET /v1/admin/buyers` → `list[AdminBuyerOut]` (profile fields + aggregate),
  ordered by `id`. Admins additionally get per-review detail later if
  moderation needs it — YAGNI now.

## Riders (carried minors from the disputes branch)

(a) **Dispute-creation race 500 → 409.** Concurrent duplicate
`POST /jobs/{id}/dispute` hits the `disputes.job_id` UNIQUE constraint and
500s. Catch `IntegrityError`, roll back, return the sequential path's 409
("job already disputed"). PG-gated race test (SQLite serializes).

(b) **`transfer_to_seller` key-seam inconsistency.** `FakeProvider.
transfer_to_seller` appends to `transfer_keys` *before* its fail checks
(`fake.py:115`), so failed attempts record keys; the other seams record only
on success. Align: record after the fail checks, like `charge_buyer`/`refund`.
Fix any tests that (accidentally) relied on failed-attempt keys by asserting
attempt behavior explicitly via the existing seams.

(c) **Dead charge-only `related_id` fallback.**
`stripe_provider.parse_webhook` builds dispute `related_id` from
`payment_intent` **or** `charge` (`stripe_provider.py:206`), but the consumer
(`api.py:1381`) matches `Payment.provider_payment_id`, which is always a
PaymentIntent id — the charge fallback can never match and silently produces
a no-op "unknown charge" path. Delete the fallback (`payment_intent` only);
a dispute event without a PI id already takes the recorded-by-dedup no-op
path honestly.

(d) **DB CHECK on adjustment amounts** — folded into migration #6 (see §Data
model).

## Testing

New `tests/test_seller_reviews.py`:

- Happy path: review a completed job → 200, row shape, buyer aggregate math
  (`rating_count`/`rating_sum`/`rating` property) across two reviews from
  different jobs.
- Guards, one test each: wrong seller → 404; unknown job → 404; job not
  completed → 409; duplicate → 409.
- Schema bounds: rating 0 and 6 → 422; comment > 2000 → 422.
- Surfaces: `GET /v1/buyer/me` (fresh buyer: `rating` null, counts 0; after a
  review: correct aggregate); `GET /v1/admin/buyers` lists profiles, admin-only.
- PG-gated: concurrent duplicate review → exactly one row, loser gets 409.

Riders: (a) PG-gated concurrent duplicate dispute → 409 not 500; (b) seam
change covered by adjusting any affected fake-provider tests + one assertion
that a failed transfer records no key; (c) webhook unit test: dispute event
with only a `charge` id → ignored/no-dispute (delete or repoint any test
asserting the fallback); (d) migration test — existing "migrations from
scratch" gate now counts 6, plus one test that a negative adjustment amount
is rejected by the DB (both backends — SQLite enforces CHECK constraints).

Demo: `scripts/demo.py` gains a one-line act after job completion — seller
reviews the buyer, print the buyer aggregate.

Suite gates unchanged: ruff, ruff format, pyright (bare exit codes — never
piped), SQLite + Postgres full runs, demo exit 0, migrations from scratch.

## Non-goals

- Double-blind reveal mechanics (revisit when ratings gate anything).
- Surfacing buyer ratings to sellers pre-accept.
- Matching/threshold effects, review windows, review editing/deletion,
  notifications for reviews, exposing seller-review comments to buyers.
- Generalizing the two review tables into one.
