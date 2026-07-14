"""Seller→buyer reviews: mirror of the buyer→seller review, display-only aggregate."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from marketplace.db import SessionLocal
from marketplace.entities import Adjustment, BuyerProfile, Dispute, Job
from marketplace.models import AdjustmentKind, DisputeSource


def test_seller_reviews_table_registered() -> None:
    from marketplace.entities import Base

    assert "seller_reviews" in Base.metadata.tables


def test_buyer_profile_rating_property() -> None:
    prof = BuyerProfile(id="b1")
    assert prof.rating is None
    prof.rating_count = 2
    prof.rating_sum = 7
    assert prof.rating == 3.5


def test_adjustments_amount_check_rejects_negative() -> None:
    """DB-level backstop for the ledger doctrine: amounts are positive, kind
    carries the sign. Enforced by CHECK on both backends."""

    with SessionLocal() as s:
        job = Job(
            quote_id=uuid4(),
            service_type_id="svc",
            buyer_id="b1",
            buyer_price=Decimal("10.00"),
        )
        s.add(job)
        s.flush()
        dispute = Dispute(job_id=job.id, source=DisputeSource.BUYER, buyer_id="b1", reason="x")
        s.add(dispute)
        s.flush()
        s.add(
            Adjustment(
                job_id=job.id,
                dispute_id=dispute.id,
                kind=AdjustmentKind.REFUND,
                amount=Decimal("-1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
