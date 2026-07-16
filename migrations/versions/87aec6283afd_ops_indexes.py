"""ops indexes

Revision ID: 87aec6283afd
Revises: ce07e913bc82
Create Date: 2026-07-15 22:55:22.155126

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "87aec6283afd"
down_revision: str | Sequence[str] | None = "ce07e913bc82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("ix_seller_profiles_provider_account_id"),
        "seller_profiles",
        ["provider_account_id"],
    )
    op.create_index(op.f("ix_payouts_provider_transfer_id"), "payouts", ["provider_transfer_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_payouts_provider_transfer_id"), table_name="payouts")
    op.drop_index(op.f("ix_seller_profiles_provider_account_id"), table_name="seller_profiles")
