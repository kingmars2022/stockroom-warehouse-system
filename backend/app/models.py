import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Role(str, enum.Enum):
    admin = "admin"
    supervisor = "supervisor"
    employee = "employee"


class MovementKind(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class ExpenseStatus(str, enum.Enum):
    submitted = "submitted"
    approved = "approved"
    rejected = "rejected"
    paid = "paid"


class SupplierStatus(str, enum.Enum):
    preferred = "preferred"
    backup = "backup"
    paused = "paused"


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    cognito_sub: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class Item(IdMixin, TimestampMixin, Base):
    __tablename__ = "items"

    sku: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str] = mapped_column(String(100), default="Uncategorized")
    location: Mapped[str] = mapped_column(String(100), default="Unassigned")
    unit: Mapped[str] = mapped_column(String(24), default="pcs")
    quantity_on_hand: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    minimum_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Supplier(IdMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    contact: Mapped[str] = mapped_column(String(320), default="")
    lead_days: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(default=0)
    status: Mapped[SupplierStatus] = mapped_column(Enum(SupplierStatus, name="supplier_status"), default=SupplierStatus.backup)


class StockMovement(IdMixin, Base):
    __tablename__ = "stock_movements"

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[MovementKind] = mapped_column(Enum(MovementKind, name="movement_kind"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Purchase(IdMixin, Base):
    __tablename__ = "purchases"

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="RESTRICT"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    received_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    invoice_number: Mapped[str] = mapped_column(String(120), default="")
    receipt_key: Mapped[str | None] = mapped_column(String(512))
    price_change_percent: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Expense(IdMixin, TimestampMixin, Base):
    __tablename__ = "expenses"

    submitter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    supplier: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_key: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ExpenseStatus] = mapped_column(Enum(ExpenseStatus, name="expense_status"), default=ExpenseStatus.submitted)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    actor_role: Mapped[Role] = mapped_column(Enum(Role, name="audit_role"), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProcessedSubmission(Base):
    """One row per supplier price submission that has already been applied.

    The webhook's idempotency key only stops a retry from queueing a second S3
    object. It says nothing about consumption: the object is deleted after it
    is applied, so a supplier retrying later, a delete that fails after the
    commit, or two ingest calls racing the same object would each raise the
    alert again. This row is written in the same transaction as the audit
    entry, so the record of having processed a submission cannot outlive or
    precede its effect.

    Keyed by supplier as well as submission id — two suppliers that happen to
    pick the same idempotency key are unrelated events.
    """

    __tablename__ = "processed_submissions"

    dedup_key: Mapped[str] = mapped_column(String(320), primary_key=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sku: Mapped[str] = mapped_column(String(80), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
