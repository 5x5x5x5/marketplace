"""Notification preferences: per-kind mutes, money-only must-send floor.
Spec: 2026-07-14-notification-preferences-design.md."""

from marketplace.models import EventKind, UserRole


def test_mutes_table_registered() -> None:
    from marketplace.entities import Base

    assert "notification_mutes" in Base.metadata.tables


def test_kind_roles_covers_every_kind() -> None:
    """A future kind added without a role mapping must fail fast (same
    pattern as the every-kind-has-a-renderer invariant)."""
    from marketplace.notifications import KIND_ROLES

    assert set(KIND_ROLES) == set(EventKind)
    assert set(KIND_ROLES.values()) <= {UserRole.BUYER, UserRole.SELLER, UserRole.ADMIN}


def test_must_send_is_the_money_floor() -> None:
    from marketplace.notifications import KIND_ROLES, MUST_SEND

    assert {
        EventKind.REFUND_ISSUED_BUYER,
        EventKind.DISPUTE_RESOLVED_BUYER,
        EventKind.DISPUTE_RESOLVED_SELLER,
        EventKind.PAYOUT_FAILED_ADMIN,
    } == MUST_SEND
    assert set(KIND_ROLES) >= MUST_SEND
