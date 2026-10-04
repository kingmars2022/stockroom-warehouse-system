"""Movement and purchase history is read a page at a time.

Both answered with everything ever recorded. Measured over a simulated 10,000
item warehouse that was 16 MB and 12 MB on every sign-in -- larger than the
catalogue whose fetch naming the rows had just made unnecessary.

Paging the purchase list takes one answer away from the console, which is why
`/api/price-alerts` is here: the alerts, and the alternative prices beside each
one, were derived by filtering every purchase the console had been sent.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models import Item, MovementKind, Purchase, StockMovement, Supplier, SupplierStatus, SystemSetting


@pytest.fixture()
def widget(db):
    record = Item(sku="WID-1", name="Widget", quantity_on_hand=1000, minimum_quantity=10, unit="pcs")
    db.add(record)
    db.commit()
    return record


# Every row is given the same created_at on purpose. That is the realistic case
# -- a batch of issues in one shift, and on SQLite a batch in one statement --
# and the awkward one: the timestamp alone is a partial order, and two pages cut
# from a partial order can repeat one row and never show another.
TIED = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def issues(db, item, actor, count: int, at: datetime = TIED) -> None:
    db.add_all([StockMovement(item_id=item.id, kind=MovementKind.outbound, quantity=1, actor_id=actor.id, recipient="Dispatch", note=f"run {index}", created_at=at) for index in range(count)])
    db.commit()


def purchases(db, item, supplier, actor, count: int, price_change: float = 0) -> None:
    db.add_all([Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=actor.id, quantity=1, unit_cost=10 + index, currency="CAD", invoice_number=f"INV-{index}", price_change_percent=price_change, created_at=TIED) for index in range(count)])
    db.commit()


# --------------------------------------------------------------------------
# Movements
# --------------------------------------------------------------------------

def test_a_page_of_movements_reports_the_length_of_the_whole_history(api, supervisor, widget, db):
    issues(db, widget, supervisor, 30)

    response = api.act_as(supervisor).get("/api/movements", params={"limit": 10})

    assert len(response.json()) == 10
    assert response.headers["X-Total-Count"] == "30"


def test_paging_movements_walks_the_history_once(api, supervisor, widget, db):
    issues(db, widget, supervisor, 25)

    seen = []
    for offset in (0, 10, 20):
        seen += [row["id"] for row in api.act_as(supervisor).get("/api/movements", params={"limit": 10, "offset": offset}).json()]

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a page boundary repeated a row and lost another"
    # Every row shares a timestamp, so the id is what is actually ordering
    # them. Ascending ids are the visible proof that it is in the ORDER BY:
    # without it the rows come back in whatever order the table yields, and
    # these ids are random.
    assert seen == sorted(seen), "the order is partial, so the pages are not cut from one list"


def test_an_employees_page_and_count_are_both_their_own(api, employee, supervisor, widget, db):
    """The count has to carry the same restriction as the page, or an employee
    is told there are more of their movements than they can ever reach."""
    issues(db, widget, supervisor, 12)
    issues(db, widget, employee, 3)

    response = api.act_as(employee).get("/api/movements", params={"limit": 10})

    assert len(response.json()) == 3
    assert response.headers["X-Total-Count"] == "3"


def test_asking_for_more_movements_than_a_page_holds_is_refused(api, supervisor):
    assert api.act_as(supervisor).get("/api/movements", params={"limit": 501}).status_code == 422
    assert api.act_as(supervisor).get("/api/movements", params={"offset": -1}).status_code == 422


# --------------------------------------------------------------------------
# Purchases
# --------------------------------------------------------------------------

def test_a_page_of_purchases_reports_the_length_of_the_whole_history(api, supervisor, widget, supplier, db):
    purchases(db, widget, supplier, supervisor, 22)

    response = api.act_as(supervisor).get("/api/purchases", params={"limit": 10})

    assert len(response.json()) == 10
    assert response.headers["X-Total-Count"] == "22"


def test_paging_purchases_walks_the_history_once(api, supervisor, widget, supplier, db):
    purchases(db, widget, supplier, supervisor, 25)

    seen = []
    for offset in (0, 10, 20):
        seen += [row["id"] for row in api.act_as(supervisor).get("/api/purchases", params={"limit": 10, "offset": offset}).json()]

    assert len(set(seen)) == 25
    assert seen == sorted(seen), "the order is partial, so the pages are not cut from one list"


# --------------------------------------------------------------------------
# Price alerts
# --------------------------------------------------------------------------

def test_only_purchases_past_the_threshold_are_alerts(api, supervisor, widget, supplier, db):
    purchases(db, widget, supplier, supervisor, 3, price_change=2.5)
    purchases(db, widget, supplier, supervisor, 2, price_change=31.0)

    response = api.act_as(supervisor).get("/api/price-alerts")

    assert len(response.json()) == 2
    assert all(row["price_change_percent"] == 31.0 for row in response.json())
    assert response.headers["X-Total-Count"] == "2"


def test_the_alert_count_describes_every_alert_not_the_page(api, supervisor, widget, supplier, db):
    """The dashboard puts this number on a card. Counting the page would make
    it read "5 price alerts" however many there really are."""
    purchases(db, widget, supplier, supervisor, 9, price_change=40.0)

    response = api.act_as(supervisor).get("/api/price-alerts", params={"limit": 5})

    assert len(response.json()) == 5
    assert response.headers["X-Total-Count"] == "9"


def test_the_threshold_decides_what_counts_as_an_alert(api, admin, supervisor, widget, supplier, db):
    purchases(db, widget, supplier, supervisor, 1, price_change=9.0)

    assert api.act_as(supervisor).get("/api/price-alerts").json() == []

    api.act_as(admin).patch("/api/price-policy", json={"threshold_percent": 5})

    assert len(api.act_as(supervisor).get("/api/price-alerts").json()) == 1


def test_an_alert_carries_what_the_other_suppliers_last_charged(api, supervisor, widget, supplier, db):
    """The console used to work this out by scanning every purchase it held,
    which a page cannot answer. Only the latest price per other supplier, and
    never the supplier the alert is about."""
    rival = Supplier(name="Rival Supply", lead_days=2, rating=4.0, status=SupplierStatus.backup)
    third = Supplier(name="Third Supply", lead_days=6, rating=3.5, status=SupplierStatus.backup)
    db.add_all([rival, third])
    db.commit()
    now = datetime.now(timezone.utc)
    db.add_all([
        Purchase(item_id=widget.id, supplier_id=rival.id, received_by_id=supervisor.id, quantity=1, unit_cost=7.00, currency="CAD", created_at=now - timedelta(days=30)),
        Purchase(item_id=widget.id, supplier_id=rival.id, received_by_id=supervisor.id, quantity=1, unit_cost=8.25, currency="CAD", created_at=now - timedelta(days=2)),
        Purchase(item_id=widget.id, supplier_id=third.id, received_by_id=supervisor.id, quantity=1, unit_cost=9.50, currency="CAD", created_at=now - timedelta(days=5)),
        Purchase(item_id=widget.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=1, unit_cost=14.00, currency="CAD", price_change_percent=40.0, created_at=now),
    ])
    db.commit()

    alert = api.act_as(supervisor).get("/api/price-alerts").json()[0]
    quoted = {option["supplier_name"]: option["unit_cost"] for option in alert["alternatives"]}

    assert alert["supplier_name"] == supplier.name
    assert supplier.name not in quoted, "the alert quoted the supplier it is complaining about"
    assert quoted == {"Rival Supply": 8.25, "Third Supply": 9.50}, "only the latest price per other supplier"


def test_an_alert_quotes_at_most_two_alternatives(api, supervisor, widget, supplier, db):
    rivals = [Supplier(name=f"Rival {index}", lead_days=2, rating=4.0, status=SupplierStatus.backup) for index in range(4)]
    db.add_all(rivals)
    db.commit()
    db.add_all([Purchase(item_id=widget.id, supplier_id=rival.id, received_by_id=supervisor.id, quantity=1, unit_cost=5.0 + index, currency="CAD") for index, rival in enumerate(rivals)])
    purchases(db, widget, supplier, supervisor, 1, price_change=40.0)

    assert len(api.act_as(supervisor).get("/api/price-alerts").json()[0]["alternatives"]) == 2


def test_the_alternatives_cost_one_query_for_the_whole_page(api, supervisor, supplier, db):
    """One query per alert would trade a history-sized response for a pile of
    round trips, which is not an improvement."""
    def alerting_items(count: int, batch: str) -> None:
        items = [Item(sku=f"ALERT-{batch}-{index:03d}", name=f"Alerting {batch}{index:03d}", quantity_on_hand=5, minimum_quantity=1) for index in range(count)]
        db.add_all(items)
        db.commit()
        db.add_all([Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=1, unit_cost=10.0, currency="CAD", price_change_percent=40.0) for item in items])
        db.commit()

    seen: list[str] = []
    engine = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    alerting_items(1, "a")
    event.listen(engine, "before_cursor_execute", record)
    first = api.act_as(supervisor).get("/api/price-alerts").json()
    one_alert = len(seen)
    seen.clear()
    event.remove(engine, "before_cursor_execute", record)

    alerting_items(11, "b")
    event.listen(engine, "before_cursor_execute", record)
    many = api.act_as(supervisor).get("/api/price-alerts").json()
    twelve_alerts = len(seen)
    event.remove(engine, "before_cursor_execute", record)

    assert len(first) == 1 and len(many) == 12
    assert twelve_alerts == one_alert, f"{one_alert} queries for 1 alert, {twelve_alerts} for 12"


def test_employees_cannot_read_price_alerts(api, employee):
    assert api.act_as(employee).get("/api/price-alerts").status_code == 403


def test_a_settings_row_does_not_stop_the_alerts_reading_the_threshold(api, admin, supervisor, widget, supplier, db):
    db.add(SystemSetting(key="price_alert_threshold_percent", value="25", updated_by_id=admin.id))
    db.commit()
    purchases(db, widget, supplier, supervisor, 1, price_change=20.0)

    assert api.act_as(supervisor).get("/api/price-alerts").json() == []
