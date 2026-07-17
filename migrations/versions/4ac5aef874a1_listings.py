"""listings

Revision ID: 4ac5aef874a1
Revises: 87aec6283afd
Create Date: 2026-07-16 22:29:06.878541

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4ac5aef874a1"
down_revision: str | Sequence[str] | None = "87aec6283afd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seller_id", sa.String(length=128), nullable=False),
        sa.Column("service_type_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=4000), nullable=False),
        sa.Column("photo_urls", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["service_type_id"], ["service_types.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_listings_seller_id"), "listings", ["seller_id"], unique=False)
    op.create_index(
        op.f("ix_listings_service_type_id"), "listings", ["service_type_id"], unique=False
    )
    op.add_column(
        "quotes",
        sa.Column("listing_id", sa.Uuid(), sa.ForeignKey("listings.id"), nullable=True),
    )
    op.add_column("quotes", sa.Column("pinned_seller_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("pinned_seller_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "pinned_seller_id")
    op.drop_column("quotes", "pinned_seller_id")
    op.drop_column("quotes", "listing_id")
    op.drop_index(op.f("ix_listings_service_type_id"), table_name="listings")
    op.drop_index(op.f("ix_listings_seller_id"), table_name="listings")
    op.drop_table("listings")
