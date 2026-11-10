---
name: endpoint-or-page
description: Add or change an HTTP endpoint (JSON under /v1) or a server-rendered page in this marketplace without leaking one side's money or identity to the other. Covers picking the view family (buyer/seller/admin/catalog), response_model instead of hand-built dicts, identity from the principal, catalog reads through catalog.py, CommitRoute on every router, Jinja autoescape, notification payloads, and the asymmetry tests to add. Use for any new route, response field, template, or notification kind.
---

# Endpoints and pages: who may see what

The business model is the spread: the buyer never learns the seller's payout,
the seller never learns the buyer's price, and neither sees the margin. That
secrecy lives in the response models, not in handler logic, so every rule
below is about choosing and testing the right model.

## 1. Pick the router and the view family

| Caller | Router (prefix) | Response models | Must never carry |
|---|---|---|---|
| buyer | `buyer_router` (`/v1`) | `Buyer*View`/`Buyer*Out` | `seller_payout`, `margin`, clawback amounts |
| seller | `seller_router` (`/v1/seller`) | `Seller*View`/`Seller*Out` | `buyer_price`, `margin`, refund amounts, `buyer_id`/buyer email |
| admin | `admin_router` (`/v1/admin`) | `Admin*Out`, `TransactionOut` | (sees everything) |
| anyone | catalog (`/v1/catalog`), pages (`/`) | `Catalog*Out` | payout, margin, capacity, tier, email, `payments_ready` |

Admin-only models are never returned from a buyer or seller route. Finding F1
was exactly that: the seller's completion endpoint declared
`response_model=TransactionOut`, so every seller receipt carried the buyer
price and the margin, and the demo printed it as if it were normal. A seller
view also never carries buyer identity (a review view once leaked `buyer_id`
to sellers; fixed by giving it no party ids at all).

## 2. Write the handler

- **Declare `response_model=`** with a view and return the ORM object or the
  view; never return a hand-built dict for anything role-scoped. A dict skips
  the model's field list, which is the whole enforcement mechanism. ORM
  entities never cross the API boundary unmapped.
- **Identity comes from the dependency** (`BuyerId`, `SellerId`, `AdminId`),
  never from a body or query field. No `buyer_id`/`seller_id` in a request
  model, ever: that is impersonation.
- **Ownership:** load the row, compare its owner to the principal, and answer
  the same way the neighbouring endpoints for that resource do (the dispute
  and review GETs answer 404 for "not yours" so ids can't be probed).
- **Status codes:** a POST that creates a durable resource returns 201; a
  POST that acts on an existing one returns 200 (finding F4;
  `test_creation_posts_declare_201_action_posts_declare_200` pins it).
- **Commit rules:** the router has `route_class=CommitRoute` (a new router
  without it fails at import). Flush before returning a created or mutated
  ORM object. If the handler touches money or status, also run the
  `money-path` skill.
- **Notifications:** enqueue with `notifications.enqueue`/`enqueue_admins` in
  the same transaction, with a payload built for its recipient: a seller
  payload has no `buyer_price`, a buyer payload no `seller_payout`. A new
  `EventKind` needs a renderer (`test_every_kind_has_a_renderer`).
- **Errors and logs:** a `detail` string is a response too; don't put the
  other side's amount in it. The access log carries method/path/status only.

## 3. Catalog reads and pages

- Visibility, bookability, and the public "from" price live once in
  `catalog.py` (`visible_listings_stmt`, `listing_bookable`,
  `seller_publicly_visible`, `indicative_buyer_price`, the view builders).
  Both `/v1/catalog/*` and the pages (`pages/`) call it. An earlier draft hand-rolled
  the visibility check in two places and they drifted twice; a new consumer
  (feed, admin page, search) extends `catalog.py` instead.
- `pages/` never imports `api.py` or `routers/` (cycle); move shared logic down into a
  module both import.
- Pages render from the same `Catalog*Out` objects, so the template context
  carries no field the JSON view wouldn't.
- **Autoescape is the XSS boundary** for seller-written titles,
  descriptions, and display names on unauthenticated pages. No `|safe`, no
  `Markup`, no HTML built in Python (the edit guard denies `|safe` and
  disabled autoescape; `Markup` is on you and the reviewer). Attribute values (`alt=`, `title=`) are a separate escaping
  context; quote them.
- Cookie and bearer auth resolve through the same `auth_sessions` rows; a
  page needing the principal uses the same resolver, never a parallel one.

## 4. Tests to add (first)

- **An asymmetry assertion for the new surface**, in
  `tests/test_information_asymmetry.py` style: call it as the role and assert
  the forbidden keys are absent from the JSON, recursively. F1 lived as long
  as it did because asymmetry tests covered job and offer views but not the
  completion receipt.
- The other role (and an anonymous caller) is refused.
- For a page: extend `tests/test_pages.py::test_page_source_asymmetry*` (a
  literal string scan of the rendered HTML) and the `test_xss_*` checks with
  a hostile title.
- For a notification: assert the drained payload per recipient.

Then ask for the `asymmetry-reviewer` subagent on the diff, and run the
`gate` skill.
