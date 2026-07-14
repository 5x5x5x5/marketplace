"""Transactional-outbox notifications: enqueue, renderers, drain, emitters."""


def test_notifications_table_registered() -> None:
    from marketplace.entities import Base

    assert "notifications" in Base.metadata.tables
