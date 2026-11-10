# Try-out findings — 2026-11-10 (booking pages)

Browser try-out of the booking pages phase: headless Chromium driven by
Playwright against a real uvicorn on a seeded scratch SQLite database
(`scripts/seed.py`), fake payment provider with `FAKE_CHARGES_PENDING=true`
and `ALLOW_UNSIGNED_WEBHOOKS=true`. Two browser contexts, so the buyer and
the seller each had their own cookie session.

The story driven: a buyer signs up through `/signup`, opens a listing from the
catalog, gets a price, confirms the booking; the seller logs in through
`/login`, opens the dashboard, accepts the offer (the charge stays pending);
the buyer's open booking page notices the change by itself and shows the
payment step; a dev webhook (`payment_succeeded` for the pending charge)
settles it and the buyer's page moves to "Confirmed" by itself; the seller
marks the job complete; the buyer sees "Completed" on the booking and in "My
bookings". The script also watched every page for console errors, uncaught
exceptions and 5xx responses, and checked the booking page for horizontal
scroll at phone width.

## T1 — Catalog cards had no link to their listing

- **Surfaced:** first browser run. The script could not find any link from a
  catalog card to `/listings/{id}` and had to open the listing by URL. A real
  buyer could not get from the catalog to a listing, so could not book at
  all.
- **Where:** `templates/_listing_card.html` (since the pages foundation
  phase); used on `/` and `/sellers/{id}`.
- **Severity:** Important. It blocked the whole browser booking flow; the API
  and every in-process test were unaffected.
- **Why it was missed:** the page tests fetched `/listings/{id}` directly and
  asserted on content; nothing navigated the way a person does.
- **Fix shape:** the card title is a link to the listing, stretched over the
  whole card with CSS so the card is the click target while the title stays
  the accessible name. Regression test
  `tests/test_pages.py::test_catalog_cards_link_to_their_listing`.
- **Status:** FIXED (this branch). The second full run had no findings.

## Verified good

- The full story, twice, on fresh databases: no console errors, no page
  errors, no 5xx. The htmx status poll picked up both the seller's acceptance
  and the webhook settlement within one 4 s tick and reloaded the page.
- **CSRF in a real browser.** An attacker page served from
  `http://127.0.0.1:8766` auto-submitted a cancel form (no token) to the
  buyer's booking on `http://127.0.0.1:8765`. A different port is the same
  *site*, so `SameSite=Lax` did not stop the browser: the request carried the
  session cookie. The CSRF check answered 403 and the booking stayed
  "Waiting for Seller to accept". This is the case the token exists for.
- **Double submit.** After booking, the browser's Back button to the consumed
  quote landed on the same booking; re-POSTing the confirm form answered 303
  to the same booking, and no second job was created.
- No Stripe script loaded in fake mode; the awaiting-payment page showed the
  pending state instead.
- The booking page has no horizontal scroll at 390 px width.

## Not exercised

- **The live Stripe Payment Element flow.** There is no Stripe account on this
  machine. What is tested: the publishable key and client secret reach the
  template only while a payment is pending and only for the job's buyer, they
  disappear after settlement, no Stripe script loads in fake mode, and startup
  refuses a Stripe secret key without a `pk_` publishable key. Mounting the
  element, `stripe.confirmPayment`, the redirect back and the
  `payment_intent.succeeded` webhook against real Stripe have not been run.
- Postgres for the browser run (the suite itself runs on Postgres too); the
  pages add no new locking, so SQLite was used for the walk-through.
