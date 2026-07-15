"""Moderation: suspension, content takedown, reports. Spec: 2026-07-14-moderation-design.md."""


def test_moderation_schema_registered() -> None:
    from marketplace.entities import Base

    assert "reports" in Base.metadata.tables
    users = Base.metadata.tables["users"]
    assert "status" in users.c and "suspended_reason" in users.c and "suspended_at" in users.c
    assert "comment_hidden" in Base.metadata.tables["reviews"].c
    assert "comment_hidden" in Base.metadata.tables["seller_reviews"].c


def test_public_comment_property_is_the_invariant_home() -> None:
    """Non-admin serializations read public_comment; hiding nulls it, nothing else."""
    from marketplace.entities import Review, SellerReview

    for cls in (Review, SellerReview):
        row = cls(rating=3, comment="rude text")
        assert row.public_comment == "rude text"
        row.comment_hidden = True
        assert row.public_comment is None
        assert row.comment == "rude text"  # the row itself is untouched
        row.comment_hidden = False
        assert row.public_comment == "rude text"
