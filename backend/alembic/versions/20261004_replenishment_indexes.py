"""Index the two tables the replenishment engine aggregates over.

Revision ID: 20261004_repl_indexes
Revises: 20260917_receipt_version
Create Date: 2026-10-04
"""

from alembic import op

revision = "20261004_repl_indexes"
down_revision = "20260917_receipt_version"
branch_labels = None
depends_on = None

# The engine now groups outbound movement per item inside the database and
# picks the current price per item/supplier pair with a window function. Both
# read far fewer rows than before, but on an unindexed history both still start
# from a full scan, which grows with how long the warehouse has been running.
ADDED = (
    ("stock_movements", "ix_stock_movements_kind_created_at", ["kind", "created_at"]),
    ("stock_movements", "ix_stock_movements_created_at", ["created_at"]),
    ("purchases", "ix_purchases_item_id_created_at", ["item_id", "created_at"]),
    ("purchases", "ix_purchases_item_id_supplier_id_created_at", ["item_id", "supplier_id", "created_at"]),
)

# Both of the new purchases indexes lead with item_id, which leaves this one
# nothing to serve while still costing a write on every receipt.
SUBSUMED = ("purchases", "ix_purchases_item_id", ["item_id"])


def upgrade() -> None:
    for table, name, columns in ADDED:
        op.create_index(name, table, columns)
    table, name, _ = SUBSUMED
    op.drop_index(name, table_name=table)


def downgrade() -> None:
    table, name, columns = SUBSUMED
    op.create_index(name, table, columns)
    for table, name, _ in ADDED:
        op.drop_index(name, table_name=table)
