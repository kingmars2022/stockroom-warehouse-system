"""Track consumed supplier price submissions so ingest is idempotent.

Revision ID: 20260915_processed
Revises: 20260825_initial
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260915_processed"
down_revision = "20260825_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_submissions",
        sa.Column("dedup_key", sa.String(length=320), primary_key=True),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(length=80), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_submissions")
