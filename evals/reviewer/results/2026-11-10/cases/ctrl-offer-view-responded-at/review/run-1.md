No findings.

The diff adds one field, `responded_at: datetime | None`, to `SellerOfferView`. It is a timestamp on the seller's own offer row. It carries no money, no buyer identity and no margin.

- **Route and model:** `GET /v1/seller/offers` uses `response_model=list[SellerOfferView]`. The handler is scoped by the `seller_id` principal dependency and returns `Offer` ORM rows, which Pydantic maps to the view.
- **Where the field is written:** `entities.py:240` defines it and the services set it. They set it on accept, decline and expiry, and the two expiry sites are `expire_open_offers` and `sweep_expired_offers`. The expiry timestamps reveal nothing about the buyer's price or the platform margin.
- **Existing asymmetry test:** a test already asserts `buyer_price` is absent from the offers list and that `seller_payout` is present. Because the new field is a plain timestamp, I don't think a new test is needed.

Checked: `src/marketplace/models.py` (`SellerOfferView`), `src/marketplace/routers/seller.py` (`list_offers`), `src/marketplace/services/offers.py` and `src/marketplace/services/jobs.py` (the `responded_at` writes), `src/marketplace/entities.py` (`Offer.responded_at`), and the asymmetry tests that touch `/v1/seller/offers`. No enqueue sites, templates or catalog code are touched by this diff.
