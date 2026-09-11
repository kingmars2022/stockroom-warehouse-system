from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .models import ExpenseStatus, MovementKind, Role, SupplierStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MeResponse(BaseModel):
    id: UUID
    email: str
    name: str
    role: Role


class ItemCreate(BaseModel):
    sku: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=200)
    category: str = Field(default="Uncategorized", max_length=100)
    location: str = Field(default="Unassigned", max_length=100)
    unit: str = Field(default="pcs", max_length=24)
    quantity_on_hand: int = Field(default=0, ge=0)
    minimum_quantity: int = Field(default=0, ge=0)


class ItemResponse(ORMModel):
    id: UUID
    sku: str
    name: str
    category: str
    location: str
    unit: str
    quantity_on_hand: int
    minimum_quantity: int
    created_at: datetime
    updated_at: datetime


class MovementCreate(BaseModel):
    item_id: UUID
    kind: MovementKind
    quantity: int = Field(gt=0)
    recipient: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)


class MovementResponse(ORMModel):
    id: UUID
    item_id: UUID
    kind: MovementKind
    quantity: int
    actor_id: UUID
    actor_name: str
    recipient: str
    note: str
    created_at: datetime


class SupplierCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    contact: str = Field(default="", max_length=320)
    lead_days: int = Field(default=0, ge=0)
    rating: float = Field(default=0, ge=0, le=5)
    status: SupplierStatus = SupplierStatus.backup


class SupplierResponse(ORMModel):
    id: UUID
    name: str
    contact: str
    lead_days: int
    rating: float
    status: SupplierStatus


class PurchaseCreate(BaseModel):
    item_id: UUID
    supplier_id: UUID
    quantity: int = Field(gt=0)
    unit_cost: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    invoice_number: str = Field(default="", max_length=120)
    receipt_key: str | None = Field(default=None, max_length=512)


class PurchaseResponse(ORMModel):
    id: UUID
    item_id: UUID
    supplier_id: UUID
    received_by_id: UUID
    quantity: int
    unit_cost: float
    currency: str
    invoice_number: str
    receipt_key: str | None
    price_change_percent: float
    created_at: datetime


class ExpenseCreate(BaseModel):
    item_id: UUID | None = None
    supplier: str = Field(min_length=2, max_length=200)
    quantity: int = Field(gt=0)
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    purpose: str = Field(min_length=3, max_length=2000)
    receipt_key: str | None = Field(default=None, max_length=512)


class ExpenseResponse(ORMModel):
    id: UUID
    submitter_id: UUID
    submitter_name: str
    item_id: UUID | None
    supplier: str
    quantity: int
    amount: float
    currency: str
    purpose: str
    receipt_key: str | None
    status: ExpenseStatus
    reviewer_id: UUID | None
    reviewer_name: str | None
    created_at: datetime
    updated_at: datetime


class ExpenseStatusUpdate(BaseModel):
    status: ExpenseStatus


class PricePolicyUpdate(BaseModel):
    threshold_percent: int = Field(ge=1, le=100)


class SupplierRecommendation(BaseModel):
    supplier_id: UUID
    supplier_name: str
    unit_cost: float
    currency: str
    lead_days: int
    rating: float
    score: int


class ReplenishmentRecommendation(BaseModel):
    item_id: UUID
    item_name: str
    sku: str
    unit: str
    quantity_on_hand: int
    daily_usage: float
    days_of_cover: float | None
    suggested_quantity: int
    recommended_supplier: SupplierRecommendation | None
    alternatives: list[SupplierRecommendation]


class AuditResponse(ORMModel):
    id: UUID
    actor_id: UUID
    actor_name: str
    actor_role: Role
    action: str
    target_type: str
    target_id: str
    detail: str
    created_at: datetime


class UploadIntent(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=100)
    size_bytes: int = Field(gt=0)


class UploadResponse(BaseModel):
    key: str
    upload_url: str
    expires_in_seconds: int
