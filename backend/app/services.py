import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil

import boto3
from fastapi import HTTPException, status
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from .audit_events import build_document, publish
from .cache import REPLENISHMENT_KEY, REPLENISHMENT_PREFIX, get_cache
from .config import get_settings
from .models import AuditLog, Expense, ExpenseStatus, Item, MovementKind, ProcessedSubmission, Purchase, Role, StockMovement, Supplier, SupplierStatus, SystemSetting, User


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


AUDIT_BUFFER_KEY = "pending_audit_events"


def write_audit(db: Session, actor: User, action: str, target_type: str, target_id: str, detail: str, payload: dict | None = None) -> None:
    """Record an audit entry relationally, and stage the document form of it.

    The row is the system of record and lands in the caller's transaction. The
    document is only buffered here — `_publish_audit_events` ships it once that
    transaction commits, so a rollback leaves no orphan event behind.
    """
    event_id = uuid.uuid4()
    db.add(AuditLog(id=event_id, actor_id=actor.id, actor_role=actor.role, action=action, target_type=target_type, target_id=target_id, detail=detail))
    db.info.setdefault(AUDIT_BUFFER_KEY, []).append(
        build_document(
            event_id=str(event_id),
            actor_id=str(actor.id),
            actor_role=actor.role.value,
            actor_name=actor.name,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            payload=payload,
        )
    )


@event.listens_for(Session, "after_commit")
def _publish_audit_events(session: Session) -> None:
    """Publish only when the outermost transaction commits.

    `after_commit` also fires when a SAVEPOINT is released, and every business
    function here writes its audit inside `db.begin_nested()`. Publishing on
    that signal sends the event while the outer transaction is still open, so a
    later failure — a constraint violation, a failed commit — leaves an event
    describing a change that never landed. `in_nested_transaction()` is True
    for the savepoint release and False for the real commit.
    """
    if session.in_nested_transaction():
        return
    publish(session.info.pop(AUDIT_BUFFER_KEY, []))


@event.listens_for(Session, "after_rollback")
def _discard_audit_events(session: Session) -> None:
    session.info.pop(AUDIT_BUFFER_KEY, None)


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
    write_audit(db, actor, "updated_price_alert_policy", "system_setting", PRICE_THRESHOLD_KEY, f"Price threshold changed from {previous}% to {threshold_percent}%", payload={"previous_percent": previous, "new_percent": threshold_percent})
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
        write_audit(
            db, actor,
            "received_stock" if kind is MovementKind.inbound else "issued_stock",
            "item", str(item.id),
            f"{quantity} {item.unit} {'received from' if kind is MovementKind.inbound else 'issued to'} {recipient}",
            payload={"item_id": str(item.id), "item_name": item.name, "kind": kind.value, "quantity": quantity, "unit": item.unit, "recipient": recipient, "quantity_on_hand_after": item.quantity_on_hand},
        )
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
        write_audit(
            db, actor, "received_purchase", "purchase", str(purchase.id),
            f"{quantity} {item.unit} at {unit_cost:.2f} {currency.upper()}; price change {change}%",
            payload={"item_id": str(item.id), "item_name": item.name, "supplier_id": str(supplier_id), "quantity": quantity, "unit_cost": float(unit_cost), "previous_unit_cost": previous_cost, "price_change_percent": change, "currency": currency.upper(), "invoice_number": invoice_number},
        )
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
        write_audit(
            db, actor, "submitted_reimbursement", "expense", str(expense.id),
            f"{values['amount']:.2f} {values['currency'].upper()} submitted",
            payload={"amount": float(values["amount"]), "currency": values["currency"].upper(), "status": ExpenseStatus.submitted.value, "has_receipt": bool(receipt_key)},
        )
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
        previous_status = expense.status
        expense.status = next_status
        expense.reviewer_id = actor.id
        write_audit(
            db, actor, f"expense_{next_status.value}", "expense", str(expense.id),
            f"Expense status changed to {next_status.value}",
            payload={"amount": float(expense.amount), "currency": expense.currency, "from_status": previous_status.value, "to_status": next_status.value},
        )
    db.commit()
    db.refresh(expense)
    return expense


PRICE_SUBMISSION_PREFIX = "price-submissions/"


def ingest_supplier_prices(db: Session, actor: User) -> list[dict]:
    """Drain the webhook inbox and raise an alert on every material price rise.

    The webhook Lambda only authenticates and parks the submission; it has no
    route to the database and no idea what the item last cost. That comparison
    happens here, against the same threshold an administrator sets in the
    console, and it writes through the ordinary audit path so a supplier-pushed
    rise is recorded exactly like one noticed during a purchase.

    Each submission is deleted only after its transaction commits, so a crash
    mid-batch leaves the rest of the inbox to be picked up next time rather
    than silently dropping it.
    """
    if actor.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators can ingest supplier prices")

    settings = get_settings()
    if not settings.receipt_bucket_name:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supplier price intake is not configured")

    client = boto3.client("s3", region_name=settings.aws_region)
    listing = client.list_objects_v2(Bucket=settings.receipt_bucket_name, Prefix=PRICE_SUBMISSION_PREFIX)
    threshold = get_price_threshold(db)

    results = []
    for entry in listing.get("Contents", []):
        key = entry["Key"]
        try:
            submission = json.loads(client.get_object(Bucket=settings.receipt_bucket_name, Key=key)["Body"].read())
        except Exception:
            results.append({"key": key, "status": "unreadable"})
            continue

        results.append(apply_price_submission(db, actor, submission, threshold))
        client.delete_object(Bucket=settings.receipt_bucket_name, Key=key)

    return results


def apply_price_submission(db: Session, actor: User, submission: dict, threshold: int) -> dict:
    item = db.scalar(select(Item).where(Item.sku == submission.get("sku")))
    if item is None:
        return {"sku": submission.get("sku"), "status": "unknown_sku"}

    supplier = db.get(Supplier, uuid.UUID(submission["supplier_id"])) if _is_uuid(submission.get("supplier_id")) else None
    if supplier is None:
        return {"sku": item.sku, "status": "unknown_supplier"}

    dedup_key = f"{supplier.id}:{submission.get('submission_id') or ''}"
    if submission.get("submission_id") and db.get(ProcessedSubmission, dedup_key) is not None:
        return {"sku": item.sku, "status": "duplicate"}

    quoted = float(submission["unit_cost"])
    currency = str(submission["currency"]).upper()
    previous = db.scalar(select(Purchase).where(Purchase.item_id == item.id).order_by(Purchase.created_at.desc()).limit(1))

    # Only compare like with like. Subtracting 10 USD from 13.5 CAD produces a
    # 35% "rise" that is an artefact of the exchange rate, and there is no rate
    # stored here to convert with — so the quote is recorded, not alerted on.
    comparable = previous is not None and previous.currency.upper() == currency
    previous_cost = float(previous.unit_cost) if comparable else 0
    change = round(((quoted - previous_cost) / previous_cost) * 100, 1) if previous_cost else 0
    breached = bool(previous_cost) and change >= threshold

    write_audit(
        db, actor,
        "supplier_price_alert" if breached else "supplier_price_quoted",
        "item", str(item.id),
        f"{supplier.name} quoted {quoted:.2f} {str(submission['currency']).upper()} for {item.sku}; change {change}%",
        payload={
            "sku": item.sku, "item_id": str(item.id), "supplier_id": str(supplier.id), "supplier_name": supplier.name,
            "quoted_unit_cost": quoted, "previous_unit_cost": previous_cost, "price_change_percent": change,
            "currency": currency, "threshold_percent": threshold,
            "previous_currency": previous.currency.upper() if previous else None,
            "comparable": comparable,
            "threshold_breached": breached, "submission_id": submission.get("submission_id"),
        },
    )
    if submission.get("submission_id"):
        db.add(ProcessedSubmission(dedup_key=dedup_key, supplier_id=supplier.id, sku=item.sku))
    db.commit()
    return {"sku": item.sku, "status": "alert" if breached else "recorded", "price_change_percent": change}


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def assert_receipt_ownership(key: str, actor: User) -> None:
    if not key.startswith(f"receipts/{actor.id}/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Receipt key does not belong to the current user")
    assert_receipt_validated(key)


def assert_receipt_validated(key: str) -> None:
    """Refuse to attach a receipt the upload validator has not cleared.

    The presign endpoint can only check what the client declares; the bytes go
    straight from the browser to S3. A Lambda inspects the object on
    ObjectCreated and tags it, and this is the gate that makes that verdict
    mean something.

    With no bucket configured (local demo, tests) there is nothing to read, so
    the check stands aside rather than blocking the whole flow.
    """
    settings = get_settings()
    if not settings.receipt_bucket_name:
        return

    try:
        tags = boto3.client("s3", region_name=settings.aws_region).get_object_tagging(Bucket=settings.receipt_bucket_name, Key=key)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt was not found in storage")

    verdict = {tag["Key"]: tag["Value"] for tag in tags.get("TagSet", [])}.get("validation")
    if verdict is None:
        # Validation is asynchronous, so an attach that races the upload is a
        # retry, not a rejection.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Receipt is still being validated, retry shortly")
    if verdict != "passed":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Receipt failed upload validation and cannot be attached")
