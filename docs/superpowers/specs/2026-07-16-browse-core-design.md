# Browse core — design (market-platform fork, phase 1)

**Date:** 2026-07-16
**Status:** Approved under the maintainer's standing autonomous authorization
(2026-07-16: "brainstorm the browse foundation, and then go with your
recommendations … go ahead without me"). Product shape fixed by the
maintainer: **browse-family marketplace with a few big sellers**; vertical
deliberately unspecified. Frontend decision already made: HTMX/Jinja in the
monolith (phase 2); this phase is the backend the pages will stand on.

## Problem

The template is a **dispatch** marketplace: the buyer requests a service type
and the platform's matching strategy picks the seller. A browse marketplace
inverts discovery: buyers see a catalog of seller offerings and choose. The
template has no catalog, no public read surface (everything but
`/healthz`/webhook is auth-gated), no way to book a *chosen* seller, and
demo-tuned timing (2-minute offers) that no human seller can meet.

## Decisions (maintainer-delegated, recommended options)

1. **Listings are presentation over existing service types** — pricing stays
   platform-owned (the differentiator is untouched). A `Listing` never
   carries a seller-set price; the buyer price is computed by the existing
   pipeline, the payout likewise. The catalog shows a live indicative
   `price_from`.
2. **Photos are URLs, not uploads.** With a few curated sellers, listing
   images are operator/seller-hosted URLs (`photo_urls: list[str]`,
   https-only, max 12). No object storage, no upload endpoints, no media
   port — deferred until a real seller population demands self-serve
   uploads. This deletes the body-cap/middleware complications entirely.
3. **Chosen-seller booking pins the quote; dispatch stays fully working.**
   `POST /v1/quotes` gains optional `listing_id`; a pinned quote produces an
   offer to exactly that seller. Unpinned quotes behave exactly as today —
   the template's dispatch mode remains intact underneath (and tested).
4. **The offer round stays** (no instant-book yet): big professional sellers
   keep accept/decline control. Instead, fork timing defaults become
   human-paced: `quote_ttl_minutes` 5→15, `offer_ttl_minutes` 2→120.
   Instant-book is a future toggle if sellers want it.
5. **Two visibility flags with different owners** (moderation parity):
   `active` (seller-owned toggle) and `hidden` (admin takedown — the seller
   cannot un-hide). Public catalog shows `active AND NOT hidden` only.
6. **Public catalog is unauthenticated, read-only, and asymmetry-guarded**:
   it never carries payouts, capacities, emails, or any seller internals.
   `seller_id` (opaque uuid hex, already visible to buyers on jobs) is the
   public seller identifier; no slugs (YAGNI).

## Design

### 1. `Listing` entity + migration #11

```python
class Listing(Base):
    """A seller's public offering of a service type — presentation only.
    Pricing stays platform-owned: a listing never carries a price."""

    __tablename__ = "listings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    service_type_id: Mapped[str] = mapped_column(
        ForeignKey("service_types.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(4000), default="")
    photo_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(default=True)      # seller-owned
    hidden: Mapped[bool] = mapped_column(default=False)     # admin-owned takedown
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)
```

`Quote` gains two nullable pin columns: `listing_id` (FK listings.id) and
`pinned_seller_id` (String(128), snapshot of the listing's seller at quote
time). **`Job` gains `pinned_seller_id` too** (nullable) — the quote row is
DELETED when the job is created, so the pin must travel onto the job or the
decline/expiry machinery (which only sees the job) cannot distinguish pinned
from dispatch. Migration #11 = `listings` table + two quote columns + one job
column (all nullable, no backfill). No unique constraint on
(seller, service_type) — multiple presentation variants are harmless.

### 2. Seller listing management (`/v1/seller/listings`)

- `POST /v1/seller/listings` (201) — body `{service_type_id, title,
  description?, photo_urls?}`. Guards: `_require_active` (suspension gates
  acquisition), service type must exist (404), seller must be
  `payments_ready` (409 — never list what can't be booked). Photo URLs
  validated https, ≤12.
- `GET /v1/seller/listings` — own listings, all flags visible.
- `PUT /v1/seller/listings/{id}` — own listing only (404 not-yours pattern);
  may update title/description/photo_urls/active. Cannot touch `hidden`.
- No DELETE — `active=false` is the off switch (hide-not-delete house
  pattern).
- `SellerListingOut`: id, service_type_id, title, description, photo_urls,
  active, hidden, created_at, updated_at.

### 3. Admin moderation

- `GET /v1/admin/listings` (paginated, existing idiom) — everything,
  including hidden/inactive.
- `POST /v1/admin/listings/{id}/hide` and `/unhide` — audit-logged
  (`hide_listing`/`unhide_listing`), mirroring review takedown.

### 4. Public catalog (`/v1/catalog/…`, unauthenticated, CommitRoute like all `/v1`)

- `GET /v1/catalog/listings` — paginated; optional `service_type_id` filter;
  `active AND NOT hidden` AND seller currently bookable (not suspended,
  `payments_ready`). Each item: `CatalogListingOut {id, title, description,
  photo_urls, service_type_id, price_from, seller: CatalogSellerOut}`.
- `GET /v1/catalog/listings/{listing_id}` — same shape, 404 when not
  publicly visible.
- `GET /v1/catalog/sellers/{seller_id}` — `CatalogSellerProfileOut
  {seller_id, display_name, rating, rating_count, completed_jobs,
  listings: [CatalogListingOut…]}` (visible listings only; 404 for
  unknown/suspended/not-onboarded sellers).
- `CatalogSellerOut {seller_id, display_name, rating, rating_count}`.
- **`price_from`**: computed live through the pure pricing core (buyer-side
  pipeline over the service type's base price with current supply/demand),
  quantized `to_money`. Indicative — the real quote may floor-bump upward;
  the catalog labels it "from". Cheap at few-big-sellers scale (paginated,
  pure-python pipeline, no extra queries beyond the config snapshot).
- **Asymmetry guard is a tested invariant**: a recursive key-scan test
  asserts no catalog response ever contains `seller_payout`, `margin`,
  `buyer_price` (as a field), `capacity`, `email`, or `payments_ready`.

### 5. Chosen-seller booking

`QuoteRequest` gains `listing_id: UUID | None = None`.

**Quote time (pinned):** load the listing; it must be publicly bookable
(active, not hidden, seller not suspended, `payments_ready`) — else 409
`"listing unavailable"`. `service_type_id` comes FROM the listing (the
request's `service_type_id` becomes optional; if both given they must agree —
422). Price runs the normal buyer pipeline; the floor check probes **the
pinned seller's payout only** (not the market minimum), same
`required_spread` invariant and whole-unit bump. Persist `listing_id` +
`pinned_seller_id` on the quote.

**Job time (pinned quote):** re-validate the listing is still bookable and
the seller still has an availability row for the service type — else **410
"listing no longer available"** (quote consumed, no charge made, buyer
re-browses). Otherwise create the offer directly to `pinned_seller_id`
(no matching strategy). Capacity is NOT pre-checked (offers never consume
capacity; accept enforces it under the row lock, unchanged).

**Decline/expiry of a pinned offer:** the job **expires** — no re-match. The
buyer chose this seller; silently substituting another would betray the
browse contract. Mechanism: `_match_and_offer` short-circuits when
`job.pinned_seller_id` is set and an offer for that seller already exists —
job → EXPIRED (with the existing buyer notification). Both callers (decline
handler and `_sweep_expired_offers`) flow through it, so one guard covers
both paths. (`sellers_seen_for_job` machinery untouched for dispatch.)

**Request validation matrix:** `listing_id` present + `service_type_id`
absent → OK (type comes from the listing); both present and agreeing → OK;
both present and disagreeing → 422; both absent → 422
("listing_id or service_type_id required"). `QuoteRequest.service_type_id`
becomes `str | None = None` accordingly; `QuoteOut` gains nullable
`listing_id`/`pinned_seller_id` so phase-2 pages can render the pin.

**Dispatch mode unchanged:** `listing_id` absent → exactly today's behavior,
bit for bit. Existing tests must pass unmodified (except the two TTL-default
tests below, if any assert the old defaults).

### 6. Fork timing defaults

`settings.py`: `quote_ttl_minutes: int = 15`, `offer_ttl_minutes: int = 120`.
Env-tunable as before. This is a deliberate fork divergence from the
template (recorded in CLAUDE.md); demo keeps working because it never waits
out real TTLs (the one offer-expiry test drives clocks via DB writes, not
sleeps — verify and adapt if it assumed 2 minutes).

### 7. Demo + seed + docs

- `scripts/seed.py`: each demo seller gets a listing per service type
  (title/description/photo placeholder URLs) so phase-2 pages render a real
  catalog immediately.
- `scripts/demo.py` act 9: seller creates a listing → public catalog shows it
  with `price_from` → buyer books THAT listing → pinned offer → accept →
  complete → catalog hides when seller deactivates. Assert the pinned offer
  landed on the chosen seller.
- `CLAUDE.md`: fork identity paragraph (market-platform, browse-family, few
  big sellers, upstream = template) + new non-negotiables: listings carry no
  prices; public catalog never carries payouts/capacity/emails; `hidden` is
  admin-owned, `active` seller-owned; pinned jobs never re-match.
- `README.md`: short "Fork: browse model" section + catalog/listing endpoint
  additions. Full re-branding deferred until the vertical is named.
- `docs/tryout-findings…` untouched (template history).

## Testing

- Listing CRUD guards: suspension 403, not-onboarded 409, https/max-12 photo
  validation 422, not-yours 404, PUT cannot flip `hidden`.
- Visibility matrix: (active, hidden, suspended-seller, not-onboarded) ×
  catalog list/detail/seller-profile — exactly the bookable combination is
  visible.
- Asymmetry key-scan over all three catalog responses.
- `price_from` equals the quote price for the same moment (no floor bump
  case) and is labeled indicative in the schema description.
- Pinned booking: happy path lands the offer on the chosen seller (with a
  second, cheaper seller available — proves matching was bypassed); floor
  probes pinned payout (a cheap rival can't satisfy the floor for an
  expensive pinned seller — bump math against the pinned payout); 409 at
  quote for each unavailable reason; 410 at job when deactivated between
  quote and job; decline → expired, never re-matched; dispatch regression
  (no listing_id → strategy path, existing tests).
- Both backends; fresh-volume migration chain = 11.
- Demo act 9 asserts as §7.

## Non-goals

- Photo uploads / object storage (URLs only, revisit with self-serve sellers).
- Instant-book (offer round stays; future toggle).
- Search/filters beyond `service_type_id` (few sellers — a grid, not a
  search problem).
- Slugs/SEO-pretty ids (phase-2 pages can mint display slugs client-side if
  wanted; ids are uuids).
- Any pricing-model change — platform sets both sides, unchanged.
- Frontend (phase 2), real-mail configuration, verification gate (phase 2+).
