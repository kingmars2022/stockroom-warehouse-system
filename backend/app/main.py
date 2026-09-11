import re
import uuid

import boto3
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from .auth import get_current_user, require_roles
from .config import get_settings
from .db import get_db
from .models import AuditLog, Expense, Item, MovementKind, Purchase, Role, StockMovement, Supplier, User
from .schemas import AuditResponse, ExpenseCreate, ExpenseResponse, ExpenseStatusUpdate, ItemCreate, ItemResponse, MeResponse, MovementCreate, MovementResponse, PricePolicyUpdate, PurchaseCreate, PurchaseResponse, ReplenishmentRecommendation, SupplierCreate, SupplierResponse, UploadIntent, UploadResponse
from .cache import get_cache
from .services import cached_replenishment_recommendations, create_expense, get_price_threshold, invalidate_replenishment_cache, record_movement, record_purchase, update_expense_status, update_price_threshold, write_audit

settings = get_settings()


app = FastAPI(title="Stockroom API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])


def movement_response(movement: StockMovement, actor_name: str) -> MovementResponse:
    return MovementResponse(
        id=movement.id,
        item_id=movement.item_id,
        kind=movement.kind,
        quantity=movement.quantity,
        actor_id=movement.actor_id,
        actor_name=actor_name,
        recipient=movement.recipient,
        note=movement.note,
        created_at=movement.created_at,
    )


def expense_response(expense: Expense, submitter_name: str, reviewer_name: str | None = None) -> ExpenseResponse:
    return ExpenseResponse(
        id=expense.id,
        submitter_id=expense.submitter_id,
        submitter_name=submitter_name,
        item_id=expense.item_id,
        supplier=expense.supplier,
        quantity=expense.quantity,
        amount=expense.amount,
        currency=expense.currency,
        purpose=expense.purpose,
        receipt_key=expense.receipt_key,
        status=expense.status,
        reviewer_id=expense.reviewer_id,
        reviewer_name=reviewer_name,
        created_at=expense.created_at,
        updated_at=expense.updated_at,
    )


def audit_response(audit: AuditLog, actor_name: str) -> AuditResponse:
    return AuditResponse(
        id=audit.id,
        actor_id=audit.actor_id,
        actor_name=actor_name,
        actor_role=audit.actor_role,
        action=audit.action,
        target_type=audit.target_type,
        target_id=audit.target_id,
        detail=audit.detail,
        created_at=audit.created_at,
    )


@app.get("/health")
def health_check() -> dict:
    cache = get_cache()
    return {
        "status": "ok",
        "service": "stockroom-api",
        "cache": {"backend": cache.backend_name, "ttl_seconds": cache.ttl_seconds, **cache.stats.as_dict()},
    }


@app.get("/api/me", response_model=MeResponse)
def get_me(user: User = Depends(get_current_user)):
    return user


@app.get("/api/items", response_model=list[ItemResponse])
def list_items(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Item).order_by(Item.name)).all()


@app.post("/api/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemCreate, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.admin))):
    if db.scalar(select(Item).where(Item.sku == payload.sku)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="SKU already exists")
    with db.begin_nested():
        item = Item(**payload.model_dump())
        db.add(item)
        db.flush()
        write_audit(db, user, "created_item", "item", str(item.id), f"Created SKU {item.sku}")
    db.commit()
    db.refresh(item)
    invalidate_replenishment_cache()
    return item


@app.get("/api/movements", response_model=list[MovementResponse])
def list_movements(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    actor = aliased(User)
    query = select(StockMovement, actor.name).join(actor, StockMovement.actor_id == actor.id).order_by(StockMovement.created_at.desc())
    if user.role is Role.employee:
        query = query.where(StockMovement.actor_id == user.id)
    return [movement_response(movement, actor_name) for movement, actor_name in db.execute(query).all()]


@app.post("/api/movements", response_model=MovementResponse, status_code=status.HTTP_201_CREATED)
def create_movement(payload: MovementCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    movement = record_movement(db, user, payload.item_id, payload.kind, payload.quantity, payload.recipient, payload.note)
    return movement_response(movement, user.name)


@app.get("/api/suppliers", response_model=list[SupplierResponse])
def list_suppliers(_: User = Depends(require_roles(Role.admin, Role.supervisor)), db: Session = Depends(get_db)):
    return db.scalars(select(Supplier).order_by(Supplier.name)).all()


@app.post("/api/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierCreate, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.admin))):
    with db.begin_nested():
        supplier = Supplier(**payload.model_dump())
        db.add(supplier)
        db.flush()
        write_audit(db, user, "created_supplier", "supplier", str(supplier.id), f"Created supplier {supplier.name}")
    db.commit()
    db.refresh(supplier)
    invalidate_replenishment_cache()
    return supplier


@app.get("/api/purchases", response_model=list[PurchaseResponse])
def list_purchases(_: User = Depends(require_roles(Role.admin, Role.supervisor)), db: Session = Depends(get_db)):
    return db.scalars(select(Purchase).order_by(Purchase.created_at.desc())).all()


@app.post("/api/purchases", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_purchase(payload: PurchaseCreate, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.admin, Role.supervisor))):
    return record_purchase(db, user, **payload.model_dump())


@app.get("/api/price-policy")
def read_price_policy(_: User = Depends(require_roles(Role.admin, Role.supervisor)), db: Session = Depends(get_db)):
    return {"threshold_percent": get_price_threshold(db)}


@app.patch("/api/price-policy")
def set_price_policy(payload: PricePolicyUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.admin))):
    return {"threshold_percent": update_price_threshold(db, user, payload.threshold_percent)}


@app.get("/api/replenishment-recommendations", response_model=list[ReplenishmentRecommendation])
def list_replenishment_recommendations(_: User = Depends(require_roles(Role.admin, Role.supervisor)), db: Session = Depends(get_db)):
    return cached_replenishment_recommendations(db)


@app.get("/api/expenses", response_model=list[ExpenseResponse])
def list_expenses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    submitter = aliased(User)
    reviewer = aliased(User)
    query = (
        select(Expense, submitter.name, reviewer.name)
        .join(submitter, Expense.submitter_id == submitter.id)
        .outerjoin(reviewer, Expense.reviewer_id == reviewer.id)
        .order_by(Expense.created_at.desc())
    )
    if user.role is Role.employee:
        query = query.where(Expense.submitter_id == user.id)
    return [expense_response(expense, submitter_name, reviewer_name) for expense, submitter_name, reviewer_name in db.execute(query).all()]


@app.post("/api/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def submit_expense(payload: ExpenseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    expense = create_expense(db, user, **payload.model_dump())
    return expense_response(expense, user.name)


@app.patch("/api/expenses/{expense_id}/status", response_model=ExpenseResponse)
def set_expense_status(expense_id: uuid.UUID, payload: ExpenseStatusUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    expense = update_expense_status(db, user, expense_id, payload.status)
    submitter_name = db.scalar(select(User.name).where(User.id == expense.submitter_id))
    return expense_response(expense, submitter_name, user.name if expense.reviewer_id else None)


@app.get("/api/audit-logs", response_model=list[AuditResponse])
def list_audit_logs(_: User = Depends(require_roles(Role.admin)), db: Session = Depends(get_db)):
    actor = aliased(User)
    query = select(AuditLog, actor.name).join(actor, AuditLog.actor_id == actor.id).order_by(AuditLog.created_at.desc())
    return [audit_response(audit, actor_name) for audit, actor_name in db.execute(query).all()]


@app.post("/api/attachments/presign", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def create_upload_intent(payload: UploadIntent, user: User = Depends(get_current_user)):
    if not settings.receipt_bucket_name:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Receipt storage is not configured")
    if payload.size_bytes > settings.max_receipt_size_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Receipt exceeds the maximum allowed size")
    if payload.content_type not in {"application/pdf", "image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported receipt file type")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", payload.filename)
    key = f"receipts/{user.id}/{uuid.uuid4()}-{safe_name}"
    client = boto3.client("s3", region_name=settings.aws_region)
    upload_url = client.generate_presigned_url("put_object", Params={"Bucket": settings.receipt_bucket_name, "Key": key, "ContentType": payload.content_type}, ExpiresIn=900, HttpMethod="PUT")
    return UploadResponse(key=key, upload_url=upload_url, expires_in_seconds=900)


@app.get("/api/attachments/download")
def create_download_intent(key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not settings.receipt_bucket_name:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Receipt storage is not configured")
    purchase = db.scalar(select(Purchase).where(Purchase.receipt_key == key))
    expense = db.scalar(select(Expense).where(Expense.receipt_key == key))
    if purchase is None and expense is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt record was not found")
    if purchase is not None and user.role is Role.employee:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employees cannot view purchase receipts")
    if expense is not None and user.role is Role.employee and expense.submitter_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot view another employee's receipt")
    client = boto3.client("s3", region_name=settings.aws_region)
    download_url = client.generate_presigned_url("get_object", Params={"Bucket": settings.receipt_bucket_name, "Key": key}, ExpiresIn=300)
    return {"download_url": download_url, "expires_in_seconds": 300}
