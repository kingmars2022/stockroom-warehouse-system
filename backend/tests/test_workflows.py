import pytest
from fastapi import HTTPException

from app.models import ExpenseStatus, Item, MovementKind, Purchase, Supplier, SupplierStatus
from app.services import create_expense, record_movement, record_purchase, replenishment_recommendations, update_expense_status


def test_outbound_movement_never_allows_negative_stock(db, employee):
    item = Item(sku="BX-001", name="Courier box", quantity_on_hand=4, minimum_quantity=1)
    db.add(item)
    db.commit()

    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, item.id, MovementKind.outbound, 5, "Dispatch", "Shipment run")

    db.refresh(item)
    assert error.value.status_code == 409
    assert item.quantity_on_hand == 4


def test_employee_cannot_receive_stock(db, employee):
    item = Item(sku="LB-001", name="Shipping labels", quantity_on_hand=4, minimum_quantity=1)
    db.add(item)
    db.commit()

    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, item.id, MovementKind.inbound, 1, "Supplier", "Delivery")

    assert error.value.status_code == 403


def test_purchase_calculates_price_change_and_increases_stock(db, supervisor):
    item = Item(sku="TP-001", name="Thermal paper", quantity_on_hand=10, minimum_quantity=2)
    supplier = Supplier(name="Parcel Supply")
    db.add_all([item, supplier])
    db.commit()
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=10, unit_cost=10, currency="USD", invoice_number="OLD", price_change_percent=0))
    db.commit()

    purchase = record_purchase(db, supervisor, item.id, supplier.id, 20, 12, "USD", "NEW", None)

    db.refresh(item)
    assert purchase.price_change_percent == 20
    assert item.quantity_on_hand == 30


def test_reimbursement_requires_supervisor_then_admin_payment(db, admin, supervisor, employee):
    expense = create_expense(db, employee, item_id=None, supplier="Local Store", quantity=1, amount=25, currency="USD", purpose="Urgent tape", receipt_key=None)

    with pytest.raises(HTTPException):
        update_expense_status(db, employee, expense.id, ExpenseStatus.approved)

    approved = update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)
    paid = update_expense_status(db, admin, approved.id, ExpenseStatus.paid)

    assert paid.status is ExpenseStatus.paid


def test_replenishment_recommends_supplier_from_price_and_lead_time(db, supervisor, employee):
    item = Item(sku="LBL-100", name="Priority shipping labels", quantity_on_hand=4, minimum_quantity=10, unit="rolls")
    fast_preferred = Supplier(name="Fast Parcel Supply", lead_days=2, rating=4.5, status=SupplierStatus.preferred)
    cheaper_slow = Supplier(name="Value Labels", lead_days=6, rating=4.0, status=SupplierStatus.backup)
    db.add_all([item, fast_preferred, cheaper_slow])
    db.commit()
    db.add_all([
        Purchase(item_id=item.id, supplier_id=fast_preferred.id, received_by_id=supervisor.id, quantity=20, unit_cost=10, currency="USD", invoice_number="FAST-1", price_change_percent=0),
        Purchase(item_id=item.id, supplier_id=cheaper_slow.id, received_by_id=supervisor.id, quantity=20, unit_cost=8, currency="USD", invoice_number="VALUE-1", price_change_percent=0),
    ])
    db.commit()
    record_movement(db, employee, item.id, MovementKind.outbound, 2, "Dispatch", "Priority parcel run")

    recommendations = replenishment_recommendations(db)

    recommendation = next(entry for entry in recommendations if entry["item_id"] == item.id)
    assert recommendation["suggested_quantity"] > 0
    assert recommendation["recommended_supplier"]["supplier_id"] == fast_preferred.id
    assert len(recommendation["alternatives"]) == 2
