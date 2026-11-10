"""webhook outbox

Revision ID: 5d1e8a3c9f20
Revises: 4ac5aef874a1
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d1e8a3c9f20"
down_revision: str | Sequence[str] | None = "4ac5aef874a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Every existing row backfills to status='applied', attempts=0: before
    this revision a row only existed after its transaction fully committed
    (an apply failure rolled the dedup row back too), so no existing event
    is half-applied or owed a retry. The nullable columns' NULL is the right
    historical value; old rows carry no payload, which is fine because
    APPLIED rows are never replayed.
    """
    with op.batch_alter_table("webhook_events") as batch_op:
        batch_op.add_column(sa.Column("object_id", sa.String(length=256), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status",
                sa.Enum(
                    "applied",
                    "failed",
                    "dead_letter",
                    name="webhookeventstatus",
                    native_enum=False,
                    length=32,
                ),
                nullable=False,
                server_default="applied",
            )
        )
        batch_op.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("last_error", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("payload", sa.JSON(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_webhook_events_object_id"), ["object_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_webhook_events_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_webhook_events_next_attempt_at"), ["next_attempt_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("webhook_events") as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_events_next_attempt_at"))
        batch_op.drop_index(batch_op.f("ix_webhook_events_status"))
        batch_op.drop_index(batch_op.f("ix_webhook_events_object_id"))
        batch_op.drop_column("payload")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("last_error")
        batch_op.drop_column("attempts")
        batch_op.drop_column("status")
        batch_op.drop_column("object_id")
