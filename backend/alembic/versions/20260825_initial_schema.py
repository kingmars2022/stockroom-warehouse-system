"""Create the initial Stockroom schema.

Revision ID: 20260825_initial
Revises:
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260825_initial"
down_revision = None
branch_labels = None
depends_on = None

# Every column is spelled out here rather than read from `Base.metadata`,
# because metadata is whatever app/models.py says at HEAD — not what the schema
# looked like at this revision. Building from it made this migration a moving
# target that absorbed later work and then collided with the migration meant to
# introduce it: `processed_submissions` was created here and failed the next
# revision with DuplicateTable, and `receipt_version_id` did the same to its own
# migration with DuplicateColumn. Both were invisible to the suite, which builds
# its schema from the ORM, and fatal to a fresh `alembic upgrade head`.
#
# A migration is a historical record. Written out, it stays one.

ENUMS = ("role", "movement_kind", "supplier_status", "expense_status", "audit_role")

# Reverse dependency order, for the downgrade.
TABLES = ("system_settings", "audit_logs", "expenses", "purchases", "stock_movements", "suppliers", "items", "users")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("cognito_sub", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("role", sa.Enum("admin", "supervisor", "employee", name="role"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_cognito_sub", "users", ["cognito_sub"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "items",
        sa.Column("sku", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("location", sa.String(length=100), nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False),
        sa.Column("minimum_quantity", sa.Integer(), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_items_name", "items", ["name"])
    op.create_index("ix_items_sku", "items", ["sku"], unique=True)

    op.create_table(
        "suppliers",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("contact", sa.String(length=320), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("status", sa.Enum("preferred", "backup", "paused", name="supplier_status"), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "stock_movements",
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Enum("inbound", "outbound", name="movement_kind"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("recipient", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_stock_movements_item_id", "stock_movements", ["item_id"])

    op.create_table(
        "purchases",
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=False),
        sa.Column("received_by_id", UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("invoice_number", sa.String(length=120), nullable=False),
        sa.Column("receipt_key", sa.String(length=512), nullable=True),
        sa.Column("price_change_percent", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["received_by_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_purchases_item_id", "purchases", ["item_id"])
    op.create_index("ix_purchases_supplier_id", "purchases", ["supplier_id"])

    op.create_table(
        "expenses",
        sa.Column("submitter_id", UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", UUID(as_uuid=True), nullable=True),
        sa.Column("supplier", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("receipt_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.Enum("submitted", "approved", "rejected", "paid", name="expense_status"), nullable=False),
        sa.Column("reviewer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["submitter_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_expenses_submitter_id", "expenses", ["submitter_id"])

    op.create_table(
        "audit_logs",
        sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_role", sa.Enum("admin", "supervisor", "employee", name="audit_role"), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("updated_by_id", UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
    # Dropping a table leaves the PostgreSQL types its columns used behind.
    for name in ENUMS:
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
