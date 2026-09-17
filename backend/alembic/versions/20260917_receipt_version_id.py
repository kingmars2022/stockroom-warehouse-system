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


def _already_present(table: str) -> bool:
    """The initial migration builds its tables from `Base.metadata`, which is
    whatever app/models.py says at HEAD rather than a snapshot of the schema at
    that revision. It therefore creates `purchases` and `expenses` with this
    column already on them, and a plain add_column fails a fresh
    `alembic upgrade head` with DuplicateColumn — invisible to every test that
    runs against an ORM-created schema, and fatal to a new deployment.

    That migration's ORIGINAL_TABLES comment fences off the same problem for
    tables; columns need the same care until it is made a real snapshot.
    """
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    for table in TABLES:
        if not _already_present(table):
            op.add_column(table, sa.Column(COLUMN, sa.String(length=256), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        if _already_present(table):
            op.drop_column(table, COLUMN)
