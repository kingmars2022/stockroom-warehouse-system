"""Create the initial Stockroom schema.

Revision ID: 20260825_initial
Revises:
Create Date: 2026-08-25
"""

from alembic import op

from app.db import Base
import app.models  # noqa: F401

revision = "20260825_initial"
down_revision = None
branch_labels = None
depends_on = None

# The tables this migration actually owns. `Base.metadata` is not a frozen
# snapshot of the schema at this revision -- it reflects whatever models
# exist in app/models.py at HEAD, so an unscoped create_all() silently grows
# to include every table added by every later migration too. That is not
# hypothetical: it created processed_submissions before the migration meant
# to (20260915_processed_submissions.py), which then failed with
# DuplicateTable on the very next line. Naming the original tables here is
# what keeps this migration a fixed historical snapshot instead of a moving
# target that collides with whatever gets added next.
ORIGINAL_TABLES = ["users", "items", "suppliers", "stock_movements", "purchases", "expenses", "audit_logs", "system_settings"]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in ORIGINAL_TABLES])


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in ORIGINAL_TABLES])
