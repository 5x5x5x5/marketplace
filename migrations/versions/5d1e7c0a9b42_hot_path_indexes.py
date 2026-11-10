"""hot path indexes

Revision ID: 5d1e7c0a9b42
Revises: 4ac5aef874a1
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d1e7c0a9b42"
down_revision: str | Sequence[str] | None = "4ac5aef874a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(op.f("ix_quotes_expires_at"), "quotes", ["expires_at"])
    op.create_index("ix_jobs_service_type_id_status", "jobs", ["service_type_id", "status"])
    op.create_index("ix_jobs_seller_id_status", "jobs", ["seller_id", "status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_jobs_seller_id_status", table_name="jobs")
    op.drop_index("ix_jobs_service_type_id_status", table_name="jobs")
    op.drop_index(op.f("ix_quotes_expires_at"), table_name="quotes")
