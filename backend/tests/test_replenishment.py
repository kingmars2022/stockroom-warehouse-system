"""Replenishment guidance: usage derivation, cover, sizing, supplier ranking."""

from datetime import datetime, timedelta, timezone

from app.models import Item, MovementKind, Purchase, StockMovement, Supplier, SupplierStatus
from app.services import replenishment_recommendations


def _purchase(db, item, supplier, actor, unit_cost, invoice):
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=actor.id, quantity=20, unit_cost=unit_cost, currency="USD", invoice_number=invoice, price_change_percent=0))
    db.commit()


def _outbound(db, item, actor, quantity, days_ago):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(StockMovement(item_id=item.id, kind=MovementKind.outbound, quantity=quantity, actor_id=actor.id, recipient="Dispatch", note="", created_at=moment))
    db.commit()


def test_item_above_minimum_with_no_demand_is_not_recommended(db):
    db.add(Item(sku="OK-1", name="Well stocked", quantity_on_hand=500, minimum_quantity=10))
    db.commit()

    assert replenishment_recommendations(db) == []


def test_item_at_or_below_minimum_is_recommended(db):
    db.add(Item(sku="LOW-1", name="Running low", quantity_on_hand=5, minimum_quantity=10))
    db.commit()

    result = replenishment_recommendations(db)

    assert len(result) == 1
    assert result[0]["sku"] == "LOW-1"
    assert result[0]["suggested_quantity"] > 0


def test_daily_usage_is_derived_from_outbound_history(db, employee):
    item = Item(sku="USE-1", name="Consumed daily", quantity_on_hand=5, minimum_quantity=10)
    db.add(item)
    db.commit()
    _outbound(db, item, employee, 10, days_ago=9)

    result = replenishment_recommendations(db)

    # 10 issued spread over a 10-day window -> ~1/day
    assert result[0]["daily_usage"] == 1.0


def test_days_of_cover_is_stock_divided_by_daily_usage(db, employee):
    item = Item(sku="COV-1", name="Cover check", quantity_on_hand=5, minimum_quantity=10)
    db.add(item)
    db.commit()
    _outbound(db, item, employee, 10, days_ago=9)

    result = replenishment_recommendations(db)

    assert result[0]["days_of_cover"] == 5.0


def test_days_of_cover_is_none_without_usage(db):
    db.add(Item(sku="NON-1", name="No usage", quantity_on_hand=0, minimum_quantity=0))
    db.commit()

    result = replenishment_recommendations(db)

    assert result == [] or result[0]["days_of_cover"] is None


def test_movements_older_than_the_lookback_window_are_ignored(db, employee):
    item = Item(sku="OLD-1", name="Stale demand", quantity_on_hand=5, minimum_quantity=10)
    db.add(item)
    db.commit()
    _outbound(db, item, employee, 900, days_ago=200)

    result = replenishment_recommendations(db)

    # The 200-day-old issue is outside the 90-day window, so usage falls back
    # to the minimum-quantity heuristic rather than an enormous daily rate.
    assert result[0]["daily_usage"] < 1


def test_suggested_quantity_covers_at_least_twice_the_minimum(db):
    db.add(Item(sku="MIN-1", name="Minimum sizing", quantity_on_hand=2, minimum_quantity=10))
    db.commit()

    result = replenishment_recommendations(db)

    assert result[0]["suggested_quantity"] >= 18


def test_cheaper_supplier_outranks_a_pricier_one_at_equal_lead_time(db, supervisor):
    item = Item(sku="RANK-1", name="Ranked item", quantity_on_hand=1, minimum_quantity=10)
    cheap = Supplier(name="Cheap Co", lead_days=3, rating=4.0, status=SupplierStatus.backup)
    pricey = Supplier(name="Pricey Co", lead_days=3, rating=4.0, status=SupplierStatus.backup)
    db.add_all([item, cheap, pricey])
    db.commit()
    _purchase(db, item, cheap, supervisor, 5, "CHEAP")
    _purchase(db, item, pricey, supervisor, 15, "PRICEY")

    result = replenishment_recommendations(db)

    assert result[0]["recommended_supplier"]["supplier_name"] == "Cheap Co"


def test_faster_supplier_outranks_a_slower_one_at_equal_price(db, supervisor):
    item = Item(sku="RANK-2", name="Lead time item", quantity_on_hand=1, minimum_quantity=10)
    fast = Supplier(name="Fast Co", lead_days=1, rating=4.0, status=SupplierStatus.backup)
    slow = Supplier(name="Slow Co", lead_days=20, rating=4.0, status=SupplierStatus.backup)
    db.add_all([item, fast, slow])
    db.commit()
    _purchase(db, item, fast, supervisor, 10, "FAST")
    _purchase(db, item, slow, supervisor, 10, "SLOW")

    result = replenishment_recommendations(db)

    assert result[0]["recommended_supplier"]["supplier_name"] == "Fast Co"


def test_paused_suppliers_are_excluded_from_options(db, supervisor):
    item = Item(sku="PAUSE-1", name="Paused supplier item", quantity_on_hand=1, minimum_quantity=10)
    active = Supplier(name="Active Co", lead_days=5, rating=4.0, status=SupplierStatus.backup)
    paused = Supplier(name="Paused Co", lead_days=1, rating=5.0, status=SupplierStatus.paused)
    db.add_all([item, active, paused])
    db.commit()
    _purchase(db, item, active, supervisor, 10, "ACTIVE")
    _purchase(db, item, paused, supervisor, 1, "PAUSED")

    result = replenishment_recommendations(db)
    names = [option["supplier_name"] for option in result[0]["alternatives"]]

    assert "Paused Co" not in names
    assert names == ["Active Co"]


def test_item_without_purchase_history_has_no_recommended_supplier(db):
    db.add(Item(sku="NOSUP-1", name="Never purchased", quantity_on_hand=0, minimum_quantity=5))
    db.commit()

    result = replenishment_recommendations(db)

    assert result[0]["recommended_supplier"] is None
    assert result[0]["alternatives"] == []


def test_only_the_latest_purchase_price_per_supplier_is_used(db, supervisor):
    item = Item(sku="PRICE-1", name="Price history", quantity_on_hand=1, minimum_quantity=10)
    supplier = Supplier(name="Only Co", lead_days=3, rating=4.0, status=SupplierStatus.backup)
    db.add_all([item, supplier])
    db.commit()
    _purchase(db, item, supplier, supervisor, 20, "OLD")
    _purchase(db, item, supplier, supervisor, 7, "NEW")

    result = replenishment_recommendations(db)
    options = result[0]["alternatives"]

    assert len(options) == 1, "one supplier should yield exactly one option"


def test_results_are_sorted_by_urgency(db, employee):
    urgent = Item(sku="URG-1", name="Urgent", quantity_on_hand=1, minimum_quantity=10)
    relaxed = Item(sku="REL-1", name="Relaxed", quantity_on_hand=9, minimum_quantity=10)
    db.add_all([urgent, relaxed])
    db.commit()
    _outbound(db, urgent, employee, 30, days_ago=9)
    _outbound(db, relaxed, employee, 5, days_ago=9)

    result = replenishment_recommendations(db)
    covers = [entry["days_of_cover"] for entry in result]

    assert covers == sorted(covers)


def test_every_recommendation_exposes_the_fields_the_api_promises(db):
    db.add(Item(sku="SHAPE-1", name="Shape check", quantity_on_hand=1, minimum_quantity=5, unit="rolls"))
    db.commit()

    entry = replenishment_recommendations(db)[0]

    for field in ("item_id", "item_name", "sku", "unit", "quantity_on_hand", "daily_usage", "days_of_cover", "suggested_quantity", "recommended_supplier", "alternatives"):
        assert field in entry, f"missing {field}"
