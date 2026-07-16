"""fee aware margin

Revision ID: ce07e913bc82
Revises: a69df93d854b
Create Date: 2026-07-15 17:22:53.899740

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ce07e913bc82"
down_revision: str | Sequence[str] | None = "a69df93d854b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column(
            "fee_estimate", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "platform_config",
        sa.Column(
            "fee_pct", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.029"
        ),
    )
    op.add_column(
        "platform_config",
        sa.Column(
            "fee_fixed", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.30"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("platform_config", "fee_fixed")
    op.drop_column("platform_config", "fee_pct")
    op.drop_column("payments", "fee_estimate")
