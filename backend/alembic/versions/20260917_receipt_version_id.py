"""Pin an attached receipt to the object version that passed validation.

Revision ID: 20260917_receipt_version
Revises: 20260915_processed
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision = "20260917_receipt_version"
down_revision = "20260915_processed"
branch_labels = None
depends_on = None

# Nullable, with no backfill: rows attached before this have no version to
# record, and the read path treats a missing one as "whatever is current" —
# exactly the behaviour they were attached under.
TABLES = ("purchases", "expenses")
COLUMN = "receipt_version_id"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column(COLUMN, sa.String(length=256), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, COLUMN)
