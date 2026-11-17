"""Defects found on a second read of the code, each pinned by the test that
would have caught it.

Most are the same shape: a request a client can legitimately send that reached
a database constraint or a missing rule, and came back as a 500 or as a wrong
answer instead of the refusal it should have been.
"""

import uuid

import pytest

from app import services
from app.models import AuditLog, Expense, ExpenseStatus, Item, Purchase, Supplier, SupplierStatus, SystemSetting


# --------------------------------------------------------------------------
# A constraint the database enforces has to be an answer, not a crash
# --------------------------------------------------------------------------

def test_a_supplier_name_already_in_use_is_a_conflict(api, admin, supplier):
    """`suppliers.name` is unique and nothing checked it, so creating a second
    "Parcel Supply Co" reached the constraint and came back as a 500."""
    response = api.act_as(admin).post("/api/suppliers", json={"name": supplier.name})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_a_reimbursement_for_an_item_that_does_not_exist_is_refused(api, employee):
    """The id went straight into a foreign key. PostgreSQL refuses the insert
    and the request dies with a 500; SQLite, which every other test runs on,
    does not enforce the key and quietly stores a dangling reference."""
    response = api.act_as(employee).post("/api/expenses", json={
        "item_id": str(uuid.uuid4()),
        "supplier": "Corner Store", "quantity": 1, "amount": 9.5, "purpose": "Replacement tape",
    })

    assert response.status_code == 404
    assert response.json()["detail"] == "Item not found"


# --------------------------------------------------------------------------
# Reimbursements
# --------------------------------------------------------------------------

def _claim(api, submitter):
    return api.act_as(submitter).post("/api/expenses", json={"supplier": "Corner Store", "quantity": 1, "amount": 40.0, "purpose": "Urgent labels"}).json()


def test_nobody_approves_their_own_reimbursement(api, supervisor):
    """A supervisor could submit a claim and approve it in the next request,
    and an administrator could then pay themselves. The rule that someone else
    signs off is the whole point of an approval step."""
    claim = _claim(api, supervisor)

    response = api.act_as(supervisor).patch(f"/api/expenses/{claim['id']}/status", json={"status": "approved"})

    assert response.status_code == 403
    assert "your own" in response.json()["detail"]


def test_nobody_rejects_their_own_reimbursement_either(api, admin):
    claim = _claim(api, admin)

    assert api.act_as(admin).patch(f"/api/expenses/{claim['id']}/status", json={"status": "rejected"}).status_code == 403


def test_someone_else_can_still_approve_it_and_the_submitter_can_then_be_paid(api, admin, supervisor):
    """The rule is about the sign-off, not about who presses "paid": a second
    person approving is what the control asks for."""
    claim = _claim(api, admin)

    assert api.act_as(supervisor).patch(f"/api/expenses/{claim['id']}/status", json={"status": "approved"}).status_code == 200
    assert api.act_as(admin).patch(f"/api/expenses/{claim['id']}/status", json={"status": "paid"}).status_code == 200


def test_a_reimbursement_is_stored_in_an_upper_case_currency(api, employee, db):
    """Purchases were upper-cased on the way in and reimbursements were not, so
    the same code could be stored as "CAD" in one table and "cad" in the other
    — and the audit entry already claimed the upper-case form."""
    api.act_as(employee).post("/api/expenses", json={"supplier": "Corner Store", "quantity": 1, "amount": 12.0, "currency": "cad", "purpose": "Parking for a pickup"})

    assert db.query(Expense).one().currency == "CAD"


@pytest.mark.parametrize("currency", ["12$", "C D", "€€€"])
def test_a_currency_has_to_be_three_letters(api, employee, currency):
    response = api.act_as(employee).post("/api/expenses", json={"supplier": "Corner Store", "quantity": 1, "amount": 12.0, "currency": currency, "purpose": "Parking for a pickup"})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Purchases and price changes
# --------------------------------------------------------------------------

def test_a_price_is_only_compared_with_one_in_the_same_currency(api, supervisor, item, supplier):
    """10.00 USD followed by 13.50 CAD is not a 35% rise, it is an exchange
    rate. The supplier-quote path already refused to compare across currencies;
    receiving a purchase did the subtraction anyway and raised the alert."""
    buy = lambda cost, currency: api.act_as(supervisor).post("/api/purchases", json={  # noqa: E731
        "item_id": str(item.id), "supplier_id": str(supplier.id), "quantity": 1, "unit_cost": cost, "currency": currency}).json()

    buy(10.0, "USD")
    in_cad = buy(13.5, "CAD")
    assert in_cad["price_change_percent"] == 0, "a different currency is a first price, not a rise"

    again_in_cad = buy(14.85, "CAD")
    assert again_in_cad["price_change_percent"] == 10.0, "compared with the last CAD price, skipping nothing"


def test_the_receipt_is_checked_before_the_item_is_locked(db, supervisor, item, supplier, monkeypatch):
    """The receipt check is a round trip to S3. It ran inside the transaction
    that holds the item's row lock, so every other issue of that item queued
    behind a network call."""
    seen = {}

    def check(key, actor, version_id=None):
        seen["locked"] = db.in_nested_transaction()

    monkeypatch.setattr(services, "assert_receipt_ownership", check)
    services.record_purchase(db, supervisor, item.id, supplier.id, 1, 5.0, "CAD", "INV-1", f"receipts/{supervisor.id}/r.pdf")

    assert seen["locked"] is False


# --------------------------------------------------------------------------
# Price policy
# --------------------------------------------------------------------------

def test_setting_the_threshold_to_what_it_already_is_changes_nothing(api, admin, db):
    """The console saves this field when focus leaves it, so tabbing through
    wrote "changed from 15% to 15%" into an audit trail that is supposed to be
    a record of changes."""
    api.act_as(admin).patch("/api/price-policy", json={"threshold_percent": 20})
    before = db.query(AuditLog).count()

    response = api.act_as(admin).patch("/api/price-policy", json={"threshold_percent": 20})

    assert response.json() == {"threshold_percent": 20}
    assert db.query(AuditLog).count() == before


# --------------------------------------------------------------------------
# Supplier price intake
# --------------------------------------------------------------------------

class Inbox:
    """Just enough of S3 for the intake: a listing, a body per key, deletes."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)

    def list_objects_v2(self, **_):
        return {"Contents": [{"Key": key} for key in sorted(self.objects)]}

    def get_object(self, Key, **_):
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Key, **_):
        self.objects.pop(Key, None)


def test_one_malformed_submission_does_not_block_the_rest_of_the_inbox(db, admin, item, supplier, monkeypatch):
    """A submission the intake could not process raised, the request died, and
    nothing was deleted — so the same object failed the next call too, and
    every submission queued behind it stayed queued for good."""
    import json

    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=admin.id, quantity=1, unit_cost=10, currency="CAD"))
    db.commit()
    good = {"supplier_id": str(supplier.id), "sku": item.sku, "unit_cost": 10.5, "currency": "CAD", "submission_id": "ok-1"}
    inbox = Inbox({
        # Valid JSON the webhook would never have parked, but anything can be
        # written under the prefix and the intake trusted all of it.
        "price-submissions/a/1-bad.json": json.dumps({"supplier_id": str(supplier.id), "sku": item.sku, "unit_cost": "a lot", "currency": "CAD"}).encode(),
        "price-submissions/a/2-list.json": b"[1, 2, 3]",
        "price-submissions/a/3-good.json": json.dumps(good).encode(),
    })
    monkeypatch.setattr(services.boto3, "client", lambda *a, **k: inbox)
    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "bucket")

    results = services.ingest_supplier_prices(db, admin)

    assert [entry["status"] for entry in results] == ["malformed", "malformed", "recorded"]
    # The good one was applied and removed; the bad ones are left where they
    # are, reported, for someone to look at rather than deleted unseen.
    assert sorted(inbox.objects) == ["price-submissions/a/1-bad.json", "price-submissions/a/2-list.json"]


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------

def test_the_most_privileged_group_decides_the_role():
    """A user in two groups got whichever Cognito happened to list first."""
    from app.auth import resolve_role
    from app.models import Role

    assert resolve_role({"cognito:groups": ["employee", "admin"]}) is Role.admin
    assert resolve_role({"cognito:groups": ["supervisor", "employee"]}) is Role.supervisor
    assert resolve_role({"cognito:groups": ["unrelated"]}) is Role.employee
    assert resolve_role({}) is Role.employee


class _Token:
    credentials = "token"


def test_an_email_that_belongs_to_another_identity_is_a_conflict(db, monkeypatch):
    """`users.email` is unique. A person re-created in Cognito keeps the email
    and gets a new subject, and signing in then met the constraint as a 500 on
    every request they made."""
    import app.auth as auth
    from fastapi import HTTPException
    from app.models import Role, User

    db.add(User(cognito_sub="the-old-identity", email="person@stockroom.test", name="Person", role=Role.employee))
    db.commit()
    monkeypatch.setattr(auth, "decode_cognito_token", lambda token: {"sub": "a-new-identity", "email": "person@stockroom.test", "name": "Person"})

    with pytest.raises(HTTPException) as refused:
        auth.get_current_user(credentials=_Token(), db=db)

    assert refused.value.status_code == 409


# --------------------------------------------------------------------------
# Company appearance
# --------------------------------------------------------------------------

def test_the_company_appearance_is_stored_and_every_role_reads_it(api, admin, employee):
    """The console called this the "company default" and kept it in one
    browser's memory: it was sent nowhere, reverted on reload, and nobody else
    ever saw it."""
    saved = api.act_as(admin).patch("/api/appearance-policy", json={"mode": "dark", "accent": "blue", "density": "compact"})
    assert saved.status_code == 200

    assert api.act_as(employee).get("/api/appearance-policy").json() == {"mode": "dark", "accent": "blue", "density": "compact"}


def test_an_appearance_nobody_has_set_reads_as_the_defaults(api, employee):
    assert api.act_as(employee).get("/api/appearance-policy").json() == {"mode": "system", "accent": "green", "density": "comfortable"}


def test_only_an_administrator_changes_the_company_appearance(api, supervisor):
    assert api.act_as(supervisor).patch("/api/appearance-policy", json={"mode": "dark"}).status_code == 403


def test_an_appearance_outside_the_offered_choices_is_refused(api, admin):
    assert api.act_as(admin).patch("/api/appearance-policy", json={"mode": "neon"}).status_code == 422


def test_changing_the_company_appearance_is_audited_once(api, admin, db):
    api.act_as(admin).patch("/api/appearance-policy", json={"mode": "dark"})
    api.act_as(admin).patch("/api/appearance-policy", json={"mode": "dark"})

    assert db.query(AuditLog).filter(AuditLog.action == "updated_company_appearance").count() == 1


def test_the_company_appearance_can_be_changed_again(api, admin):
    api.act_as(admin).patch("/api/appearance-policy", json={"mode": "dark"})
    api.act_as(admin).patch("/api/appearance-policy", json={"mode": "light", "accent": "orange"})

    assert api.act_as(admin).get("/api/appearance-policy").json() == {"mode": "light", "accent": "orange", "density": "comfortable"}


@pytest.mark.parametrize("stored", ["not json", "[1, 2]", '{"mode": "neon"}'])
def test_a_stored_appearance_that_no_longer_validates_reads_as_the_defaults(api, admin, employee, db, stored):
    """Every sign-in reads this. A bad row must not be what stops the console
    loading for everybody."""
    db.add(SystemSetting(key=services.APPEARANCE_KEY, value=stored, updated_by_id=admin.id))
    db.commit()

    answer = api.act_as(employee).get("/api/appearance-policy")

    assert answer.status_code == 200
    assert answer.json() == {"mode": "system", "accent": "green", "density": "comfortable"}


def test_the_service_refuses_a_non_administrator_whoever_calls_it(db, supervisor):
    """The route checks the role as well. This is the check that still holds
    when something other than that route calls the function."""
    with pytest.raises(Exception) as refused:
        services.update_appearance_policy(db, supervisor, {"mode": "dark", "accent": "green", "density": "comfortable"})

    assert refused.value.status_code == 403


@pytest.mark.parametrize(
    "submission",
    [
        "a string, not an object",
        {"unit_cost": 4.5, "currency": "CAD"},
        {"sku": 17, "unit_cost": 4.5, "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": True, "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": [4.5], "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": "four fifty", "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": "nan", "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": float("inf"), "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": 0, "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": -1, "currency": "CAD"},
        {"sku": "A4-PAPER", "unit_cost": 4.5},
        {"sku": "A4-PAPER", "unit_cost": 4.5, "currency": "DOLLARS"},
        {"sku": "A4-PAPER", "unit_cost": 4.5, "currency": "C4D"},
    ],
)
def test_a_parked_submission_that_cannot_be_applied_is_named_as_such(submission):
    assert services._price_submission_error(submission) is not None


@pytest.mark.parametrize("cost", [4.5, 4, "4.50"])
def test_a_parked_submission_that_can_be_applied_is_let_through(cost):
    assert services._price_submission_error({"sku": "A4-PAPER", "unit_cost": cost, "currency": "CAD"}) is None
