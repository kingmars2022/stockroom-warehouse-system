"""The dashboard's figures, counted in the database.

They were reduced over every item in the warehouse in the browser, which was
the last thing keeping the whole catalogue in the console. Three aggregates and
a short list cost the same at eight SKUs as at ten thousand.
"""

import pytest

from app.models import Item


@pytest.fixture()
def shelves(db):
    db.add_all([
        # Comfortable.
        Item(sku="OK-1", name="Plenty", quantity_on_hand=500, minimum_quantity=10, unit="pcs"),
        # Short by 48 -- the most depleted of the three.
        Item(sku="LOW-1", name="Nearly out", quantity_on_hand=2, minimum_quantity=50, unit="pcs"),
        # Short by 8.
        Item(sku="LOW-2", name="Getting low", quantity_on_hand=2, minimum_quantity=10, unit="pcs"),
        # Exactly at its minimum, which counts as low.
        Item(sku="LOW-3", name="At the line", quantity_on_hand=10, minimum_quantity=10, unit="pcs"),
    ])
    db.commit()


def test_the_summary_counts_the_catalogue_and_the_units_in_it(api, shelves):
    body = api.get("/api/items/summary").json()

    assert body["total"] == 4
    assert body["units_on_hand"] == 514


def test_an_item_at_its_minimum_counts_as_low(api, shelves):
    """`<=`, not `<`. A minimum is the level at which to reorder, not the level
    below which to panic."""
    body = api.get("/api/items/summary").json()

    assert body["low_stock_count"] == 3
    assert "At the line" in [item["name"] for item in body["low_stock"]]


def test_the_low_stock_list_leads_with_the_most_depleted(api, shelves):
    """By the shortfall against the minimum, not the raw count: 2 left of a
    minimum of 50 is more urgent than 2 of 2."""
    body = api.get("/api/items/summary").json()

    assert [item["sku"] for item in body["low_stock"]] == ["LOW-1", "LOW-2", "LOW-3"]


def test_the_low_stock_list_is_a_handful_while_the_count_is_everything(api, db):
    db.add_all([Item(sku=f"SHORT-{index:03d}", name=f"Short {index:03d}", quantity_on_hand=0, minimum_quantity=5) for index in range(40)])
    db.commit()

    body = api.get("/api/items/summary").json()

    assert body["low_stock_count"] == 40
    assert len(body["low_stock"]) == 5, "the attention queue shows a handful; the card shows the count"


def test_an_empty_warehouse_summarises_as_zeroes_rather_than_failing(api):
    """SUM over no rows is NULL, which is not a number the dashboard can print."""
    body = api.get("/api/items/summary").json()

    assert body == {"total": 0, "units_on_hand": 0, "low_stock_count": 0, "low_stock": []}


def test_the_summary_is_open_to_every_role(api, employee, shelves):
    assert api.act_as(employee).get("/api/items/summary").status_code == 200


def test_the_catalogue_can_be_narrowed_to_what_needs_attention(api, shelves):
    """The dashboard's attention card navigates to this filter, which has to be
    applied by the query now that the console holds one page of items."""
    response = api.get("/api/items", params={"low_only": "true"})

    assert sorted(item["sku"] for item in response.json()) == ["LOW-1", "LOW-2", "LOW-3"]
    assert response.headers["X-Total-Count"] == "3"
