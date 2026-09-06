"""Reimbursement approval state machine, receipt ownership, and price policy."""

import uuid

import pytest
from fastapi import HTTPException

from app.models import AuditLog, ExpenseStatus, Role, SystemSetting
from app.services import (
    PRICE_THRESHOLD_KEY,
    assert_receipt_ownership,
    create_expense,
    get_price_threshold,
    update_expense_status,
    update_price_threshold,
)


def _expense(db, actor, **overrides):
    values = dict(item_id=None, supplier="Local Store", quantity=1, amount=25, currency="USD", purpose="Urgent tape", receipt_key=None)
    values.update(overrides)
    return create_expense(db, actor, **values)


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------

def test_new_expense_starts_as_submitted(db, employee):
    assert _expense(db, employee).status is ExpenseStatus.submitted


def test_expense_records_its_submitter(db, employee):
    assert _expense(db, employee).submitter_id == employee.id


def test_creating_an_expense_writes_an_audit_row(db, employee):
    _expense(db, employee)

    assert db.query(AuditLog).filter(AuditLog.action == "submitted_reimbursement").count() == 1


def test_expense_receipt_must_belong_to_the_submitter(db, employee):
    with pytest.raises(HTTPException) as error:
        _expense(db, employee, receipt_key="receipts/other-person/file.pdf")

    assert error.value.status_code == 403


def test_expense_accepts_a_receipt_owned_by_the_submitter(db, employee):
    key = f"receipts/{employee.id}/file.pdf"

    assert _expense(db, employee, receipt_key=key).receipt_key == key


# --------------------------------------------------------------------------
# Approval transitions
# --------------------------------------------------------------------------

def test_supervisor_can_approve_a_submitted_expense(db, employee, supervisor):
    expense = _expense(db, employee)

    assert update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved).status is ExpenseStatus.approved


def test_admin_can_approve_a_submitted_expense(db, employee, admin):
    expense = _expense(db, employee)

    assert update_expense_status(db, admin, expense.id, ExpenseStatus.approved).status is ExpenseStatus.approved


def test_supervisor_can_reject_a_submitted_expense(db, employee, supervisor):
    expense = _expense(db, employee)

    assert update_expense_status(db, supervisor, expense.id, ExpenseStatus.rejected).status is ExpenseStatus.rejected


def test_employees_cannot_approve_expenses(db, employee, supervisor):
    expense = _expense(db, employee)

    with pytest.raises(HTTPException) as error:
        update_expense_status(db, employee, expense.id, ExpenseStatus.approved)

    assert error.value.status_code == 403


def test_an_expense_cannot_be_approved_twice(db, employee, supervisor):
    expense = _expense(db, employee)
    update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)

    with pytest.raises(HTTPException) as error:
        update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)

    assert error.value.status_code == 403


def test_a_rejected_expense_cannot_be_approved(db, employee, supervisor):
    expense = _expense(db, employee)
    update_expense_status(db, supervisor, expense.id, ExpenseStatus.rejected)

    with pytest.raises(HTTPException):
        update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)


# --------------------------------------------------------------------------
# Payment transitions
# --------------------------------------------------------------------------

def test_admin_can_pay_an_approved_expense(db, employee, supervisor, admin):
    expense = _expense(db, employee)
    approved = update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)

    assert update_expense_status(db, admin, approved.id, ExpenseStatus.paid).status is ExpenseStatus.paid


def test_supervisors_cannot_pay(db, employee, supervisor):
    expense = _expense(db, employee)
    approved = update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)

    with pytest.raises(HTTPException) as error:
        update_expense_status(db, supervisor, approved.id, ExpenseStatus.paid)

    assert error.value.status_code == 403


def test_a_submitted_expense_cannot_skip_straight_to_paid(db, employee, admin):
    expense = _expense(db, employee)

    with pytest.raises(HTTPException) as error:
        update_expense_status(db, admin, expense.id, ExpenseStatus.paid)

    assert error.value.status_code == 403


def test_moving_back_to_submitted_is_not_a_valid_transition(db, employee, admin):
    expense = _expense(db, employee)

    with pytest.raises(HTTPException) as error:
        update_expense_status(db, admin, expense.id, ExpenseStatus.submitted)

    assert error.value.status_code == 422


def test_transitioning_a_missing_expense_is_404(db, admin):
    with pytest.raises(HTTPException) as error:
        update_expense_status(db, admin, uuid.uuid4(), ExpenseStatus.approved)

    assert error.value.status_code == 404


def test_the_reviewer_is_recorded_on_the_expense(db, employee, supervisor):
    expense = _expense(db, employee)

    reviewed = update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)

    assert reviewed.reviewer_id == supervisor.id


def test_each_transition_writes_an_audit_row(db, employee, supervisor, admin):
    expense = _expense(db, employee)
    approved = update_expense_status(db, supervisor, expense.id, ExpenseStatus.approved)
    update_expense_status(db, admin, approved.id, ExpenseStatus.paid)

    actions = {entry.action for entry in db.query(AuditLog).all()}
    assert {"expense_approved", "expense_paid"} <= actions


# --------------------------------------------------------------------------
# Receipt ownership helper
# --------------------------------------------------------------------------

def test_receipt_ownership_accepts_the_owner_prefix(db, employee):
    assert_receipt_ownership(f"receipts/{employee.id}/x.pdf", employee)  # does not raise


def test_receipt_ownership_rejects_another_users_prefix(db, employee, admin):
    with pytest.raises(HTTPException):
        assert_receipt_ownership(f"receipts/{admin.id}/x.pdf", employee)


def test_receipt_ownership_rejects_a_path_outside_receipts(db, employee):
    with pytest.raises(HTTPException):
        assert_receipt_ownership("uploads/anything.pdf", employee)


# --------------------------------------------------------------------------
# Price alert policy
# --------------------------------------------------------------------------

def test_price_threshold_defaults_to_fifteen_percent(db):
    assert get_price_threshold(db) == 15


def test_admin_can_change_the_price_threshold(db, admin):
    assert update_price_threshold(db, admin, 25) == 25
    assert get_price_threshold(db) == 25


def test_changing_the_threshold_twice_updates_in_place(db, admin):
    update_price_threshold(db, admin, 25)
    update_price_threshold(db, admin, 30)

    assert get_price_threshold(db) == 30
    assert db.query(SystemSetting).filter(SystemSetting.key == PRICE_THRESHOLD_KEY).count() == 1


def test_supervisors_cannot_change_the_price_threshold(db, supervisor):
    with pytest.raises(HTTPException) as error:
        update_price_threshold(db, supervisor, 25)

    assert error.value.status_code == 403


def test_employees_cannot_change_the_price_threshold(db, employee):
    with pytest.raises(HTTPException):
        update_price_threshold(db, employee, 25)


def test_threshold_change_is_audited_with_the_previous_value(db, admin):
    update_price_threshold(db, admin, 40)

    entry = db.query(AuditLog).filter(AuditLog.action == "updated_price_alert_policy").one()
    assert "15%" in entry.detail and "40%" in entry.detail


def test_threshold_change_records_who_made_it(db, admin):
    update_price_threshold(db, admin, 40)

    setting = db.get(SystemSetting, PRICE_THRESHOLD_KEY)
    assert setting.updated_by_id == admin.id
