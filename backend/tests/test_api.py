"""HTTP-level tests for every endpoint, exercised through FastAPI's TestClient.

Authentication is replaced with a dependency override so these tests cover
routing, request validation, role guards, serialisation and status codes
without needing a live Cognito user pool. The Cognito claim handling itself is
covered directly in test_auth.py. The `api` client fixture lives in
conftest.py, since more than one file drives the app over HTTP now.
"""

import uuid

import pytest

from app.models import ExpenseStatus, Item, MovementKind, Supplier, SupplierStatus


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def test_health_is_ok(api):
    response = api.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_cache_backend(api):
    body = api.get("/health").json()

    assert body["cache"]["backend"] in {"memory", "redis"}
    assert "hit_rate_percent" in body["cache"]


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def test_me_returns_the_current_user(api, employee):
    body = api.get("/api/me").json()

    assert body["id"] == str(employee.id)
    assert body["role"] == "employee"


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

def test_listing_items_is_open_to_every_role(api, item):
    response = api.get("/api/items")

    assert response.status_code == 200
    assert response.json()[0]["sku"] == item.sku


def _catalogue(db, count: int, prefix: str = "PAGE") -> None:
    """Names run opposite to insertion order, so a page that comes back sorted
    by name can only have been sorted by the query."""
    db.add_all([Item(sku=f"{prefix}-{index:03d}", name=f"Item {count - index:03d}", quantity_on_hand=10, minimum_quantity=1) for index in range(count)])
    db.commit()


def test_a_page_of_the_catalogue_reports_how_many_there_are_in_all(api, db):
    """A console paging through 10,000 SKUs needs the size of the catalogue to
    know how far the pages run, and cannot get it by counting what it was sent."""
    _catalogue(db, 30)

    response = api.get("/api/items", params={"limit": 10})

    assert len(response.json()) == 10
    assert response.headers["X-Total-Count"] == "30"


def test_paging_walks_the_catalogue_once_without_repeating_or_skipping(api, db):
    _catalogue(db, 25)

    seen = []
    for offset in (0, 10, 20):
        seen += [row["name"] for row in api.get("/api/items", params={"limit": 10, "offset": offset}).json()]

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a page boundary repeated or dropped a row"
    assert seen == sorted(seen), "pages have to be cut from one order, or they overlap"


def test_search_narrows_the_page_and_the_count_with_it(api, db):
    _catalogue(db, 10)
    db.add(Item(sku="FIND-1", name="Thermal label roll", category="Labels", location="C-04-02", quantity_on_hand=5, minimum_quantity=1))
    db.commit()

    response = api.get("/api/items", params={"search": "thermal"})

    assert [row["sku"] for row in response.json()] == ["FIND-1"]
    # Counted after the filter: the count has to describe the same set the
    # pages are cut from, or the pager runs off the end of the results.
    assert response.headers["X-Total-Count"] == "1"


def test_search_matches_a_storage_location_as_typed(api, db):
    """Aisle codes are what a picker reads off the shelf, and they contain the
    character LIKE uses for a single-character wildcard."""
    db.add_all([
        Item(sku="LOC-1", name="On the shelf", location="C-04-02", quantity_on_hand=1, minimum_quantity=0),
        Item(sku="LOC-2", name="Elsewhere", location="D-11-03", quantity_on_hand=1, minimum_quantity=0),
    ])
    db.commit()

    assert [row["sku"] for row in api.get("/api/items", params={"search": "C-04"}).json()] == ["LOC-1"]
    # "_" and "%" are the person's characters, not the pattern's.
    assert api.get("/api/items", params={"search": "C_04"}).json() == []
    assert api.get("/api/items", params={"search": "%"}).json() == []


def test_a_single_request_cannot_ask_for_an_unbounded_page(api, db):
    _catalogue(db, 3)

    assert api.get("/api/items", params={"limit": 501}).status_code == 422
    assert api.get("/api/items", params={"limit": 0}).status_code == 422
    assert api.get("/api/items", params={"offset": -1}).status_code == 422


def test_the_total_count_is_exposed_to_a_browser_on_another_origin(api, item):
    """The console reads the count from the response header, and a cross-origin
    fetch cannot see a header the server does not list as exposed."""
    from app.main import app

    cors = next(middleware for middleware in app.user_middleware if "CORSMiddleware" in str(middleware.cls))

    assert "X-Total-Count" in cors.kwargs["expose_headers"]


def test_admin_can_create_an_item(api, admin):
    response = api.act_as(admin).post("/api/items", json={"sku": "NEW-1", "name": "New item", "quantity_on_hand": 5, "minimum_quantity": 1})

    assert response.status_code == 201
    assert response.json()["sku"] == "NEW-1"


def test_employees_cannot_create_items(api, employee):
    response = api.act_as(employee).post("/api/items", json={"sku": "NEW-2", "name": "New item"})

    assert response.status_code == 403


def test_duplicate_sku_is_rejected(api, admin, item):
    response = api.act_as(admin).post("/api/items", json={"sku": item.sku, "name": "Clone"})

    assert response.status_code == 409


def test_item_payload_is_validated(api, admin):
    response = api.act_as(admin).post("/api/items", json={"sku": "X", "name": "Too short a sku"})

    assert response.status_code == 422


def test_negative_stock_is_rejected_by_the_schema(api, admin):
    response = api.act_as(admin).post("/api/items", json={"sku": "NEG-1", "name": "Negative", "quantity_on_hand": -5})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Movements
# --------------------------------------------------------------------------

def test_issuing_stock_returns_the_movement(api, employee, item):
    response = api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 3, "recipient": "Dispatch"})

    assert response.status_code == 201
    assert response.json()["quantity"] == 3
    assert response.json()["actor_name"] == employee.name


def test_issuing_more_than_available_is_a_conflict(api, employee, item):
    response = api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 999, "recipient": "Dispatch"})

    assert response.status_code == 409


def test_employees_cannot_receive_stock_over_http(api, employee, item):
    response = api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "inbound", "quantity": 1, "recipient": "Supplier"})

    assert response.status_code == 403


def test_movement_against_a_missing_item_is_not_found(api, employee):
    response = api.act_as(employee).post("/api/movements", json={"item_id": str(uuid.uuid4()), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})

    assert response.status_code == 404


def test_zero_quantity_is_rejected_by_the_schema(api, employee, item):
    response = api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 0, "recipient": "Dispatch"})

    assert response.status_code == 422


def test_employees_only_see_their_own_movements(api, employee, supervisor, item, db):
    api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})
    api.act_as(supervisor).post("/api/movements", json={"item_id": str(item.id), "kind": "inbound", "quantity": 1, "recipient": "Supplier"})

    mine = api.act_as(employee).get("/api/movements").json()
    everything = api.act_as(supervisor).get("/api/movements").json()

    assert len(mine) == 1
    assert len(everything) == 2
    assert mine[0]["actor_name"] == employee.name
    assert {movement["actor_name"] for movement in everything} == {employee.name, supervisor.name}


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------

def test_admin_can_create_a_supplier(api, admin):
    response = api.act_as(admin).post("/api/suppliers", json={"name": "New Supplier", "lead_days": 4, "rating": 4.5})

    assert response.status_code == 201
    assert response.json()["name"] == "New Supplier"


def test_supervisors_cannot_create_suppliers(api, supervisor):
    response = api.act_as(supervisor).post("/api/suppliers", json={"name": "Blocked Supplier"})

    assert response.status_code == 403


def test_supervisors_can_list_suppliers(api, supervisor, supplier):
    response = api.act_as(supervisor).get("/api/suppliers")

    assert response.status_code == 200
    assert response.json()[0]["name"] == supplier.name


def test_employees_cannot_list_suppliers(api, employee):
    assert api.act_as(employee).get("/api/suppliers").status_code == 403


def test_supplier_rating_above_five_is_rejected(api, admin):
    response = api.act_as(admin).post("/api/suppliers", json={"name": "Overrated", "rating": 9})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Purchases
# --------------------------------------------------------------------------

def test_supervisor_can_record_a_purchase(api, supervisor, item, supplier):
    response = api.act_as(supervisor).post("/api/purchases", json={"item_id": str(item.id), "supplier_id": str(supplier.id), "quantity": 5, "unit_cost": 12.5, "currency": "USD", "invoice_number": "INV-9"})

    assert response.status_code == 201
    assert response.json()["quantity"] == 5


def test_employees_cannot_record_purchases_over_http(api, employee, item, supplier):
    response = api.act_as(employee).post("/api/purchases", json={"item_id": str(item.id), "supplier_id": str(supplier.id), "quantity": 1, "unit_cost": 1.0})

    assert response.status_code == 403


def test_employees_cannot_list_purchases(api, employee):
    assert api.act_as(employee).get("/api/purchases").status_code == 403


def test_admin_can_list_purchases(api, admin):
    assert api.act_as(admin).get("/api/purchases").status_code == 200


# --------------------------------------------------------------------------
# Price policy
# --------------------------------------------------------------------------

def test_price_policy_defaults_to_fifteen(api, admin):
    assert api.act_as(admin).get("/api/price-policy").json() == {"threshold_percent": 15}


def test_admin_can_update_the_price_policy(api, admin):
    response = api.act_as(admin).patch("/api/price-policy", json={"threshold_percent": 30})

    assert response.status_code == 200
    assert response.json()["threshold_percent"] == 30


def test_employees_cannot_update_the_price_policy(api, employee):
    assert api.act_as(employee).patch("/api/price-policy", json={"threshold_percent": 30}).status_code == 403


def test_price_policy_range_is_validated(api, admin):
    assert api.act_as(admin).patch("/api/price-policy", json={"threshold_percent": 500}).status_code == 422


# --------------------------------------------------------------------------
# Replenishment
# --------------------------------------------------------------------------

def test_supervisor_can_read_replenishment_guidance(api, supervisor, db):
    db.add(Item(sku="LOW-9", name="Low stock", quantity_on_hand=1, minimum_quantity=10))
    db.commit()

    response = api.act_as(supervisor).get("/api/replenishment-recommendations")

    assert response.status_code == 200
    assert response.json()[0]["sku"] == "LOW-9"


def test_employees_cannot_read_replenishment_guidance(api, employee):
    assert api.act_as(employee).get("/api/replenishment-recommendations").status_code == 403


def test_cached_replenishment_response_still_deserialises_uuids(api, supervisor, db):
    """Second read comes from the cache as JSON strings; the response model
    must still coerce them back into UUIDs rather than 500."""
    db.add(Item(sku="LOW-8", name="Low stock", quantity_on_hand=1, minimum_quantity=10))
    db.commit()

    first = api.act_as(supervisor).get("/api/replenishment-recommendations")
    second = api.act_as(supervisor).get("/api/replenishment-recommendations")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------

def test_employee_can_submit_an_expense(api, employee):
    response = api.act_as(employee).post("/api/expenses", json={"supplier": "Local Store", "quantity": 1, "amount": 25.0, "purpose": "Urgent tape"})

    assert response.status_code == 201
    assert response.json()["status"] == "submitted"
    assert response.json()["submitter_name"] == employee.name


def test_expense_purpose_is_validated(api, employee):
    response = api.act_as(employee).post("/api/expenses", json={"supplier": "Local Store", "quantity": 1, "amount": 25.0, "purpose": "x"})

    assert response.status_code == 422


def test_supervisor_can_approve_over_http(api, employee, supervisor):
    created = api.act_as(employee).post("/api/expenses", json={"supplier": "Local Store", "quantity": 1, "amount": 25.0, "purpose": "Urgent tape"}).json()

    response = api.act_as(supervisor).patch(f"/api/expenses/{created['id']}/status", json={"status": "approved"})

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["submitter_name"] == employee.name
    assert response.json()["reviewer_name"] == supervisor.name


def test_employees_cannot_approve_over_http(api, employee):
    created = api.act_as(employee).post("/api/expenses", json={"supplier": "Local Store", "quantity": 1, "amount": 25.0, "purpose": "Urgent tape"}).json()

    response = api.act_as(employee).patch(f"/api/expenses/{created['id']}/status", json={"status": "approved"})

    assert response.status_code == 403


def test_employees_only_see_their_own_expenses(api, employee, supervisor):
    api.act_as(employee).post("/api/expenses", json={"supplier": "Store A", "quantity": 1, "amount": 10.0, "purpose": "Tape for the line"})
    api.act_as(supervisor).post("/api/expenses", json={"supplier": "Store B", "quantity": 1, "amount": 20.0, "purpose": "Boxes for the line"})

    mine = api.act_as(employee).get("/api/expenses").json()
    everything = api.act_as(supervisor).get("/api/expenses").json()

    assert len(mine) == 1
    assert len(everything) == 2
    assert mine[0]["submitter_name"] == employee.name


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------

def test_admin_can_read_the_audit_log(api, admin, employee, item):
    api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})

    response = api.act_as(admin).get("/api/audit-logs")

    assert response.status_code == 200
    assert any(entry["action"] == "issued_stock" for entry in response.json())
    assert any(entry["actor_name"] == employee.name for entry in response.json())


def test_employees_cannot_read_the_audit_log(api, employee):
    assert api.act_as(employee).get("/api/audit-logs").status_code == 403


def test_audit_events_expose_the_payload_the_relational_log_flattens(api, admin, employee, item):
    api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 4, "recipient": "Dispatch"})

    response = api.act_as(admin).get("/api/audit-events")

    assert response.status_code == 200
    event = next(entry for entry in response.json() if entry["action"] == "issued_stock")
    assert event["payload"]["quantity"] == 4
    assert event["payload"]["recipient"] == "Dispatch"
    assert event["actor"]["name"] == employee.name
    assert event["target"]["id"] == str(item.id)


def test_audit_events_filter_on_a_field_only_one_action_has(api, admin, employee, item, supplier):
    api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 4, "recipient": "Dispatch"})
    api.act_as(admin).post("/api/purchases", json={"item_id": str(item.id), "supplier_id": str(supplier.id), "quantity": 2, "unit_cost": 10.0, "currency": "cad", "invoice_number": "INV-1"})

    quantities = api.act_as(admin).get("/api/audit-events", params={"min_quantity": 4})
    assert [entry["action"] for entry in quantities.json()] == ["issued_stock"]

    by_action = api.act_as(admin).get("/api/audit-events", params={"action": "received_purchase"})
    assert len(by_action.json()) == 1

    by_role = api.act_as(admin).get("/api/audit-events", params={"actor_role": "employee"})
    assert [entry["action"] for entry in by_role.json()] == ["issued_stock"]


def test_audit_events_accept_a_time_window_and_a_limit(api, admin, employee, item):
    for _ in range(3):
        api.act_as(employee).post("/api/movements", json={"item_id": str(item.id), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})

    capped = api.act_as(admin).get("/api/audit-events", params={"limit": 2})
    assert len(capped.json()) == 2

    future = api.act_as(admin).get("/api/audit-events", params={"since": "2999-01-01T00:00:00Z"})
    assert future.json() == []


def test_employees_cannot_read_audit_events(api, employee):
    assert api.act_as(employee).get("/api/audit-events").status_code == 403


def test_only_admins_can_ingest_supplier_prices(api, employee):
    assert api.act_as(employee).post("/api/supplier-prices/ingest").status_code == 403


def test_ingesting_supplier_prices_reports_what_it_processed(api, admin, item, supplier, monkeypatch):
    import json as _json

    from app import services

    class _Body:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

    submission = _json.dumps({"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 9.99, "currency": "cad"}).encode()

    class _S3:
        def list_objects_v2(self, Bucket, Prefix):
            return {"Contents": [{"Key": "price-submissions/a.json"}]}

        def get_object(self, Bucket, Key):
            return {"Body": _Body(submission)}

        def delete_object(self, Bucket, Key):
            pass

    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: _S3())

    response = api.act_as(admin).post("/api/supplier-prices/ingest")

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["results"][0]["sku"] == item.sku


def test_the_agent_endpoint_is_unavailable_without_a_provider(api, admin):
    """No provider configured is the default, including in the local demo."""
    assert api.act_as(admin).post("/api/agent/replenishment", json={}).status_code == 503


def test_employees_cannot_run_the_agent(api, employee):
    assert api.act_as(employee).post("/api/agent/replenishment", json={}).status_code == 403


def test_the_agent_returns_proposals_without_placing_an_order(api, db, admin, item, supplier, monkeypatch):
    from app import main
    from app.agent.llm import LLMResponse, ScriptedClient, ToolCall
    from app.models import Purchase

    # A proposal is only accepted for an item the engine actually flagged, from
    # a supplier that has actually supplied it — so the fixture has to be short
    # and have purchase history behind it.
    item.quantity_on_hand = 4
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=admin.id, quantity=5, unit_cost=10.0, currency="CAD", invoice_number="SEED"))
    db.commit()

    client = ScriptedClient([
        LLMResponse(tool_calls=[ToolCall("propose_purchase", {"sku": item.sku, "supplier_name": supplier.name, "quantity": 12, "reason": "low cover"})]),
        LLMResponse(text="One order proposed."),
    ])
    monkeypatch.setattr(main, "build_client", lambda _settings: client)

    before = len(api.act_as(admin).get("/api/purchases").json())

    response = api.act_as(admin).post("/api/agent/replenishment", json={"instruction": "what should we reorder?"})

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "One order proposed."
    assert body["proposals"][0]["requires_human_approval"] is True
    # The proposal exists; no purchase was placed by it.
    assert len(api.act_as(admin).get("/api/purchases").json()) == before


def test_health_reports_the_audit_event_backend(api):
    assert api.get("/health").json()["audit_events"]["backend"] == "memory"


# --------------------------------------------------------------------------
# Receipt attachments (S3 is not configured in tests)
# --------------------------------------------------------------------------

def test_presign_reports_storage_is_unconfigured(api, employee):
    response = api.act_as(employee).post("/api/attachments/presign", json={"filename": "receipt.pdf", "content_type": "application/pdf", "size_bytes": 1024})

    assert response.status_code == 503


def test_download_reports_storage_is_unconfigured(api, employee):
    response = api.act_as(employee).get("/api/attachments/download", params={"key": "receipts/x/y.pdf"})

    assert response.status_code == 503


def test_presign_payload_is_validated(api, employee):
    response = api.act_as(employee).post("/api/attachments/presign", json={"filename": "", "content_type": "application/pdf", "size_bytes": 1})

    assert response.status_code == 422
