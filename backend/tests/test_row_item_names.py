"""Every row that is about an item says which item, by name.

A movement, a purchase and a reimbursement each stored only the item's id, so
the console put names on them by holding the whole catalogue and looking each
one up. That is the one thing a warehouse with 10,000 SKUs cannot afford to
send, and it is also wrong in the small: a row about an item outside whatever
page the client happens to hold renders blank.

The names are joined, not fetched per row -- the point is that these endpoints
cost the same number of queries whatever they return.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.models import Item, Supplier, SupplierStatus


@pytest.fixture()
def rolls(db):
    record = Item(sku="LBL-500", name="Thermal label roll", quantity_on_hand=40, minimum_quantity=5, unit="rolls")
    db.add(record)
    db.commit()
    return record


@contextmanager
def counting_queries(db):
    """Counts statements sent while the block runs."""
    seen: list[str] = []
    engine = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _catalogue(db, count: int) -> None:
    """Enough items that no single page of /api/items holds them all."""
    db.add_all([Item(sku=f"BULK-{index:03d}", name=f"Bulk item {index:03d}", quantity_on_hand=5, minimum_quantity=1, unit="pcs") for index in range(count)])
    db.commit()


def test_a_movement_row_names_the_item_it_moved(api, employee, rolls):
    created = api.act_as(employee).post("/api/movements", json={"item_id": str(rolls.id), "kind": "outbound", "quantity": 2, "recipient": "Dispatch"})

    # On the way back out of the write, so the console can show the new row
    # without reloading anything.
    assert created.json()["item_name"] == "Thermal label roll"
    assert created.json()["item_unit"] == "rolls"

    listed = api.get("/api/movements").json()[0]

    assert listed["item_name"] == "Thermal label roll"
    assert listed["item_unit"] == "rolls"


def test_a_purchase_row_names_the_item_bought(api, supervisor, rolls, supplier):
    created = api.act_as(supervisor).post("/api/purchases", json={"item_id": str(rolls.id), "supplier_id": str(supplier.id), "quantity": 10, "unit_cost": 4.5, "invoice_number": "INV-1"})

    assert created.json()["item_name"] == "Thermal label roll"
    assert created.json()["item_unit"] == "rolls"

    listed = api.act_as(supervisor).get("/api/purchases").json()[0]

    assert listed["item_name"] == "Thermal label roll"
    assert listed["item_unit"] == "rolls"


def test_a_reimbursement_row_names_the_item_claimed_for(api, employee, rolls):
    created = api.act_as(employee).post("/api/expenses", json={"item_id": str(rolls.id), "supplier": "Corner Store", "quantity": 1, "amount": 18.0, "purpose": "Ran out mid-shift"})

    assert created.json()["item_name"] == "Thermal label roll"

    listed = api.act_as(employee).get("/api/expenses").json()[0]

    assert listed["item_name"] == "Thermal label roll"
    assert listed["item_unit"] == "rolls"


def test_a_reimbursement_for_nothing_catalogued_has_no_name_to_give(api, employee):
    """The only one of the three whose item is optional, so the only one whose
    name is nullable. An outer join, not an inner one."""
    created = api.act_as(employee).post("/api/expenses", json={"supplier": "Corner Store", "quantity": 1, "amount": 9.0, "purpose": "Parking for a pickup"})

    assert created.status_code == 201
    assert created.json()["item_name"] is None
    assert created.json()["item_unit"] is None

    listed = api.act_as(employee).get("/api/expenses").json()[0]

    assert listed["item_name"] is None
    assert listed["item_unit"] is None


def test_a_row_names_its_item_although_the_client_holds_none_of_the_catalogue(api, supervisor, rolls, supplier, db):
    """The whole reason for the change. The client can be holding one page of
    60 items, or none of them, and the row still reads correctly."""
    _catalogue(db, 60)
    api.act_as(supervisor).post("/api/movements", json={"item_id": str(rolls.id), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})
    api.act_as(supervisor).post("/api/purchases", json={"item_id": str(rolls.id), "supplier_id": str(supplier.id), "quantity": 1, "unit_cost": 5.0})

    page = api.get("/api/items", params={"limit": 1}).json()

    assert len(page) == 1
    assert page[0]["name"] != "Thermal label roll", "the item has to be off the page for this to prove anything"
    assert api.act_as(supervisor).get("/api/movements").json()[0]["item_name"] == "Thermal label roll"
    assert api.act_as(supervisor).get("/api/purchases").json()[0]["item_name"] == "Thermal label roll"


@pytest.mark.parametrize("endpoint", ["/api/movements", "/api/purchases", "/api/expenses"])
def test_naming_the_items_costs_no_query_per_row(api, supervisor, rolls, supplier, db, endpoint):
    """Resolving a name per row would trade a catalogue-sized response for a
    row-sized pile of queries, which is not an improvement. The count is taken
    twice over different numbers of rows: joined, it does not move.
    """
    def write_rows(count: int) -> None:
        for _ in range(count):
            api.act_as(supervisor).post("/api/movements", json={"item_id": str(rolls.id), "kind": "outbound", "quantity": 1, "recipient": "Dispatch"})
            api.act_as(supervisor).post("/api/purchases", json={"item_id": str(rolls.id), "supplier_id": str(supplier.id), "quantity": 1, "unit_cost": 5.0})
            api.act_as(supervisor).post("/api/expenses", json={"item_id": str(rolls.id), "supplier": "Corner Store", "quantity": 1, "amount": 3.0, "purpose": "Replacement roll"})

    write_rows(1)
    with counting_queries(db) as first:
        one_row = api.act_as(supervisor).get(endpoint).json()

    write_rows(9)
    with counting_queries(db) as second:
        ten_rows = api.act_as(supervisor).get(endpoint).json()

    # A purchase also records the inbound movement it caused, so the exact
    # counts differ per endpoint; what matters is that the second read is the
    # bigger one and still costs the same.
    assert len(ten_rows) > len(one_row)
    assert all(row["item_name"] == "Thermal label roll" for row in ten_rows)
    assert len(second) == len(first), f"{len(first)} queries for 1 row, {len(second)} for 10"
