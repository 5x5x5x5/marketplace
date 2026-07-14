"""Disputes, arbitration, adjustments ledger, chargebacks."""


def test_dispute_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"disputes", "adjustments"} <= set(Base.metadata.tables)
