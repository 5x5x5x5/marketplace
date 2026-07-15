"""notification prefs

Revision ID: a69df93d854b
Revises: 48d2c1099dea
Create Date: 2026-07-14 23:02:12.697354

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a69df93d854b"
down_revision: str | Sequence[str] | None = "48d2c1099dea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notification_mutes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "offer_received",
                "job_accepted_buyer",
                "job_completed_buyer",
                "job_expired_buyer",
                "job_cancelled_seller",
                "refund_issued_buyer",
                "payout_failed_admin",
                "dispute_opened_seller",
                "dispute_opened_admin",
                "dispute_resolved_buyer",
                "dispute_resolved_seller",
                "chargeback_opened_admin",
                "chargeback_closed_admin",
                "report_opened_admin",
                name="eventkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind"),
    )
    op.create_index(
        op.f("ix_notification_mutes_user_id"), "notification_mutes", ["user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_notification_mutes_user_id"), table_name="notification_mutes")
    op.drop_table("notification_mutes")
