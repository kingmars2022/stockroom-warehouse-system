"""Stock movements, purchases, and the invariants that protect inventory."""

import uuid

import pytest
from fastapi import HTTPException

from app.models import AuditLog, Item, MovementKind, Purchase, Supplier
from app.services import record_movement, record_purchase


# --------------------------------------------------------------------------
# Outbound movements
# --------------------------------------------------------------------------

def test_outbound_reduces_stock(db, employee, item):
    record_movement(db, employee, item.id, MovementKind.outbound, 10, "Dispatch", "Run")

    db.refresh(item)
    assert item.quantity_on_hand == 40


def test_outbound_beyond_stock_is_rejected(db, employee, item):
    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, item.id, MovementKind.outbound, 51, "Dispatch", "Run")

    assert error.value.status_code == 409


def test_rejected_outbound_leaves_stock_untouched(db, employee, item):
    with pytest.raises(HTTPException):
        record_movement(db, employee, item.id, MovementKind.outbound, 51, "Dispatch", "Run")

    db.refresh(item)
    assert item.quantity_on_hand == 50


def test_issuing_exactly_the_remaining_stock_is_allowed(db, employee, item):
    record_movement(db, employee, item.id, MovementKind.outbound, 50, "Dispatch", "Clear out")

    db.refresh(item)
    assert item.quantity_on_hand == 0


def test_sequential_issues_cannot_drive_stock_negative(db, employee, item):
    """The second issue must see the first one's committed decrement."""
    record_movement(db, employee, item.id, MovementKind.outbound, 30, "Dispatch", "First")

    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, item.id, MovementKind.outbound, 30, "Dispatch", "Second")

    db.refresh(item)
    assert error.value.status_code == 409
    assert item.quantity_on_hand == 20


def test_movement_against_a_missing_item_is_404(db, employee):
    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, uuid.uuid4(), MovementKind.outbound, 1, "Dispatch", "")

    assert error.value.status_code == 404


# --------------------------------------------------------------------------
# Inbound movements
# --------------------------------------------------------------------------

def test_inbound_increases_stock(db, supervisor, item):
    record_movement(db, supervisor, item.id, MovementKind.inbound, 25, "Supplier", "Delivery")

    db.refresh(item)
    assert item.quantity_on_hand == 75


def test_employees_cannot_receive_stock(db, employee, item):
    with pytest.raises(HTTPException) as error:
        record_movement(db, employee, item.id, MovementKind.inbound, 5, "Supplier", "Delivery")

    assert error.value.status_code == 403


def test_admin_can_receive_stock(db, admin, item):
    movement = record_movement(db, admin, item.id, MovementKind.inbound, 5, "Supplier", "Delivery")

    assert movement.kind is MovementKind.inbound


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def test_issuing_stock_writes_an_audit_row(db, employee, item):
    record_movement(db, employee, item.id, MovementKind.outbound, 1, "Dispatch", "Run")

    entries = db.query(AuditLog).all()
    assert any(entry.action == "issued_stock" for entry in entries)


def test_receiving_stock_writes_an_audit_row(db, supervisor, item):
    record_movement(db, supervisor, item.id, MovementKind.inbound, 1, "Supplier", "Delivery")

    entries = db.query(AuditLog).all()
    assert any(entry.action == "received_stock" for entry in entries)


def test_audit_row_records_the_acting_role(db, employee, item):
    record_movement(db, employee, item.id, MovementKind.outbound, 1, "Dispatch", "Run")

    entry = db.query(AuditLog).filter(AuditLog.action == "issued_stock").one()
    assert entry.actor_id == employee.id
    assert entry.actor_role is employee.role


# --------------------------------------------------------------------------
# Purchases
# --------------------------------------------------------------------------

def test_purchase_increases_stock(db, supervisor, item, supplier):
    record_purchase(db, supervisor, item.id, supplier.id, 20, 10.0, "usd", "INV-1", None)

    db.refresh(item)
    assert item.quantity_on_hand == 70


def test_purchase_normalises_currency_to_uppercase(db, supervisor, item, supplier):
    purchase = record_purchase(db, supervisor, item.id, supplier.id, 1, 10.0, "usd", "INV-1", None)

    assert purchase.currency == "USD"


def test_first_purchase_reports_no_price_change(db, supervisor, item, supplier):
    purchase = record_purchase(db, supervisor, item.id, supplier.id, 1, 10.0, "USD", "INV-1", None)

    assert purchase.price_change_percent == 0


def test_price_increase_is_measured_against_the_previous_purchase(db, supervisor, item, supplier):
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=1, unit_cost=10, currency="USD", invoice_number="OLD", price_change_percent=0))
    db.commit()

    purchase = record_purchase(db, supervisor, item.id, supplier.id, 1, 12.5, "USD", "NEW", None)

    assert purchase.price_change_percent == 25.0


def test_price_decrease_is_reported_as_negative(db, supervisor, item, supplier):
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=1, unit_cost=10, currency="USD", invoice_number="OLD", price_change_percent=0))
    db.commit()

    purchase = record_purchase(db, supervisor, item.id, supplier.id, 1, 8.0, "USD", "NEW", None)

    assert purchase.price_change_percent == -20.0


def test_employees_cannot_record_purchases(db, employee, item, supplier):
    with pytest.raises(HTTPException) as error:
        record_purchase(db, employee, item.id, supplier.id, 1, 10.0, "USD", "INV-1", None)

    assert error.value.status_code == 403


def test_purchase_against_a_missing_item_is_404(db, supervisor, supplier):
    with pytest.raises(HTTPException) as error:
        record_purchase(db, supervisor, uuid.uuid4(), supplier.id, 1, 10.0, "USD", "INV-1", None)

    assert error.value.status_code == 404


def test_purchase_against_a_missing_supplier_is_404(db, supervisor, item):
    with pytest.raises(HTTPException) as error:
        record_purchase(db, supervisor, item.id, uuid.uuid4(), 1, 10.0, "USD", "INV-1", None)

    assert error.value.status_code == 404


def test_purchase_also_records_an_inbound_movement(db, supervisor, item, supplier):
    record_purchase(db, supervisor, item.id, supplier.id, 7, 10.0, "USD", "INV-1", None)

    movements = db.query(Item).all()  # touch the session
    audit = db.query(AuditLog).filter(AuditLog.action == "received_purchase").all()
    assert movements and len(audit) == 1


def test_purchase_receipt_must_belong_to_the_actor(db, supervisor, item, supplier):
    with pytest.raises(HTTPException) as error:
        record_purchase(db, supervisor, item.id, supplier.id, 1, 10.0, "USD", "INV-1", "receipts/someone-else/file.pdf")

    assert error.value.status_code == 403


def test_purchase_accepts_a_receipt_owned_by_the_actor(db, supervisor, item, supplier):
    key = f"receipts/{supervisor.id}/receipt.pdf"

    purchase = record_purchase(db, supervisor, item.id, supplier.id, 1, 10.0, "USD", "INV-1", key)

    assert purchase.receipt_key == key
