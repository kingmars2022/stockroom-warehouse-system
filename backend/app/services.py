from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .cache import REPLENISHMENT_KEY, REPLENISHMENT_PREFIX, get_cache
from .models import AuditLog, Expense, ExpenseStatus, Item, MovementKind, Purchase, Role, StockMovement, Supplier, SupplierStatus, SystemSetting, User


PRICE_THRESHOLD_KEY = "price_alert_threshold_percent"


def replenishment_recommendations(db: Session) -> list[dict]:
    """Return transparent replenishment guidance from recent outbound demand."""
    now = datetime.now(timezone.utc)
    lookback_start = now - timedelta(days=90)
    items = db.scalars(select(Item).order_by(Item.name)).all()
    suppliers = {supplier.id: supplier for supplier in db.scalars(select(Supplier)).all()}
    movements = db.scalars(select(StockMovement).where(StockMovement.kind == MovementKind.outbound)).all()
    purchases = db.scalars(select(Purchase).order_by(Purchase.created_at.desc())).all()

    outbound_by_item: dict = {}
    for movement in movements:
        occurred_at = movement.created_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        if occurred_at >= lookback_start:
            outbound_by_item.setdefault(movement.item_id, []).append(movement)

    latest_purchase: dict[tuple, Purchase] = {}
    for purchase in purchases:
        latest_purchase.setdefault((purchase.item_id, purchase.supplier_id), purchase)

    options_by_item: dict = {}
    for (item_id, supplier_id), purchase in latest_purchase.items():
        supplier = suppliers.get(supplier_id)
        if supplier is None or supplier.status is SupplierStatus.paused:
            continue
        options_by_item.setdefault(item_id, []).append((supplier, purchase))

    recommendations: list[dict] = []
    for item in items:
        outbound = outbound_by_item.get(item.id, [])
        total_issued = sum(movement.quantity for movement in outbound)
        if outbound:
            earliest = min(movement.created_at for movement in outbound)
            if earliest.tzinfo is None:
                earliest = earliest.replace(tzinfo=timezone.utc)
            demand_days = max(1, min(90, (now - earliest).days + 1))
            daily_usage = total_issued / demand_days
        else:
            daily_usage = item.minimum_quantity / 30 if item.quantity_on_hand <= item.minimum_quantity and item.minimum_quantity else 0

        supplier_options = options_by_item.get(item.id, [])
        max_lead = max((supplier.lead_days for supplier, _ in supplier_options), default=0)
        days_of_cover = round(item.quantity_on_hand / daily_usage, 1) if daily_usage else None
        target_quantity = max(item.minimum_quantity * 2, ceil(daily_usage * max(14, max_lead + 7)))
        suggested_quantity = max(0, target_quantity - item.quantity_on_hand)
        needs_replenishment = item.quantity_on_hand <= item.minimum_quantity or (days_of_cover is not None and days_of_cover <= max(14, max_lead + 7))
        if not needs_replenishment:
            continue
        if suggested_quantity == 0:
            suggested_quantity = max(1, item.minimum_quantity * 2 - item.quantity_on_hand)

        lowest_cost = min((float(purchase.unit_cost) for _, purchase in supplier_options), default=0)
        slowest_lead = max((supplier.lead_days for supplier, _ in supplier_options), default=0)
        ranked = []
        for supplier, purchase in supplier_options:
            cost = float(purchase.unit_cost)
            cost_score = (lowest_cost / cost) if cost else 0
            lead_score = (slowest_lead - supplier.lead_days) / slowest_lead if slowest_lead else 1
            preferred_bonus = 0.05 if supplier.status is SupplierStatus.preferred else 0
            score = round(min(1, cost_score * 0.6 + lead_score * 0.25 + (supplier.rating / 5) * 0.15 + preferred_bonus) * 100)
            ranked.append({"supplier_id": supplier.id, "supplier_name": supplier.name, "unit_cost": cost, "currency": purchase.currency, "lead_days": supplier.lead_days, "rating": supplier.rating, "score": score})
        ranked.sort(key=lambda option: (-option["score"], option["unit_cost"], option["lead_days"]))
        recommendations.append({"item_id": item.id, "item_name": item.name, "sku": item.sku, "unit": item.unit, "quantity_on_hand": item.quantity_on_hand, "daily_usage": round(daily_usage, 2), "days_of_cover": days_of_cover, "suggested_quantity": suggested_quantity, "recommended_supplier": ranked[0] if ranked else None, "alternatives": ranked})
    return sorted(recommendations, key=lambda item: (item["days_of_cover"] is None, item["days_of_cover"] or 9999, item["item_name"]))


def invalidate_replenishment_cache() -> int:
    """Drop cached replenishment guidance. Called after any write that changes
    stock levels, purchase history, or the supplier pool."""
    return get_cache().invalidate(REPLENISHMENT_PREFIX)


def cached_replenishment_recommendations(db: Session) -> list[dict]:
    """Read-through cached view of :func:`replenishment_recommendations`.

    Values round-trip through JSON, so UUIDs come back as strings; the
    endpoint's response model coerces them back. Callers that need native
    UUIDs (tests, internal logic) should use the uncached function directly.
    """
    return get_cache().get_or_set(REPLENISHMENT_KEY, lambda: replenishment_recommendations(db))


def write_audit(db: Session, actor: User, action: str, target_type: str, target_id: str, detail: str) -> None:
    db.add(AuditLog(actor_id=actor.id, actor_role=actor.role, action=action, target_type=target_type, target_id=target_id, detail=detail))


def get_price_threshold(db: Session) -> int:
    value = db.get(SystemSetting, PRICE_THRESHOLD_KEY)
    return int(value.value) if value else 15


def update_price_threshold(db: Session, actor: User, threshold_percent: int) -> int:
    if actor.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators can update price policy")
    setting = db.get(SystemSetting, PRICE_THRESHOLD_KEY)
    previous = int(setting.value) if setting else 15
    if setting is None:
        setting = SystemSetting(key=PRICE_THRESHOLD_KEY, value=str(threshold_percent), updated_by_id=actor.id)
        db.add(setting)
    else:
        setting.value = str(threshold_percent)
        setting.updated_by_id = actor.id
    write_audit(db, actor, "updated_price_alert_policy", "system_setting", PRICE_THRESHOLD_KEY, f"Price threshold changed from {previous}% to {threshold_percent}%")
    db.commit()
    return threshold_percent


def record_movement(db: Session, actor: User, item_id, kind: MovementKind, quantity: int, recipient: str, note: str) -> StockMovement:
    if kind is MovementKind.inbound and actor.role is Role.employee:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employees cannot receive stock")
    with db.begin_nested():
        item = db.scalar(select(Item).where(Item.id == item_id).with_for_update())
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        if kind is MovementKind.outbound and item.quantity_on_hand < quantity:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Only {item.quantity_on_hand} {item.unit} are available")
        item.quantity_on_hand += quantity if kind is MovementKind.inbound else -quantity
        movement = StockMovement(item_id=item.id, kind=kind, quantity=quantity, actor_id=actor.id, recipient=recipient, note=note)
        db.add(movement)
        write_audit(db, actor, "received_stock" if kind is MovementKind.inbound else "issued_stock", "item", str(item.id), f"{quantity} {item.unit} {'received from' if kind is MovementKind.inbound else 'issued to'} {recipient}")
    db.commit()
    db.refresh(movement)
    invalidate_replenishment_cache()
    return movement


def record_purchase(db: Session, actor: User, item_id, supplier_id, quantity: int, unit_cost: float, currency: str, invoice_number: str, receipt_key: str | None) -> Purchase:
    if actor.role is Role.employee:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employees cannot receive purchases")
    with db.begin_nested():
        item = db.scalar(select(Item).where(Item.id == item_id).with_for_update())
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        if db.get(Supplier, supplier_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
        previous = db.scalar(select(Purchase).where(Purchase.item_id == item_id).order_by(Purchase.created_at.desc()).limit(1))
        if receipt_key:
            assert_receipt_ownership(receipt_key, actor)
        previous_cost = float(previous.unit_cost) if previous else 0
        change = round(((unit_cost - previous_cost) / previous_cost) * 100, 1) if previous_cost else 0
        item.quantity_on_hand += quantity
        purchase = Purchase(item_id=item_id, supplier_id=supplier_id, received_by_id=actor.id, quantity=quantity, unit_cost=Decimal(str(unit_cost)), currency=currency.upper(), invoice_number=invoice_number, receipt_key=receipt_key, price_change_percent=change)
        db.add(purchase)
        db.flush()
        db.add(StockMovement(item_id=item.id, kind=MovementKind.inbound, quantity=quantity, actor_id=actor.id, recipient="supplier", note=f"Purchase {invoice_number or 'unreferenced'}"))
        write_audit(db, actor, "received_purchase", "purchase", str(purchase.id), f"{quantity} {item.unit} at {unit_cost:.2f} {currency.upper()}; price change {change}%")
    db.commit()
    db.refresh(purchase)
    invalidate_replenishment_cache()
    return purchase


def create_expense(db: Session, actor: User, **values) -> Expense:
    receipt_key = values.get("receipt_key")
    if receipt_key:
        assert_receipt_ownership(receipt_key, actor)
    with db.begin_nested():
        expense = Expense(submitter_id=actor.id, **values)
        db.add(expense)
        db.flush()
        write_audit(db, actor, "submitted_reimbursement", "expense", str(expense.id), f"{values['amount']:.2f} {values['currency'].upper()} submitted")
    db.commit()
    db.refresh(expense)
    return expense


def update_expense_status(db: Session, actor: User, expense_id, next_status: ExpenseStatus) -> Expense:
    with db.begin_nested():
        expense = db.scalar(select(Expense).where(Expense.id == expense_id).with_for_update())
        if expense is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
        if next_status in {ExpenseStatus.approved, ExpenseStatus.rejected}:
            if actor.role not in {Role.admin, Role.supervisor} or expense.status is not ExpenseStatus.submitted:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a supervisor or administrator can review a submitted expense")
        elif next_status is ExpenseStatus.paid:
            if actor.role is not Role.admin or expense.status is not ExpenseStatus.approved:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an administrator can pay an approved expense")
        else:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This status transition is not permitted")
        expense.status = next_status
        expense.reviewer_id = actor.id
        write_audit(db, actor, f"expense_{next_status.value}", "expense", str(expense.id), f"Expense status changed to {next_status.value}")
    db.commit()
    db.refresh(expense)
    return expense


def assert_receipt_ownership(key: str, actor: User) -> None:
    if not key.startswith(f"receipts/{actor.id}/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Receipt key does not belong to the current user")
