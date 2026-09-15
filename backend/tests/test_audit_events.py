"""Audit events: the document projection of the relational audit log.

Three properties are worth protecting, and each maps to a test below:

* a committed change publishes exactly one event carrying a structured payload,
* a rolled-back change publishes nothing, and
* an event store that is down degrades querying without failing the write.

The live-MongoDB test at the bottom self-skips unless MONGO_TEST_URL points at
a real instance, the same arrangement test_concurrency.py uses for PostgreSQL.
"""

import os
import uuid

import pytest
from fastapi import HTTPException

from app.audit_events import (
    InMemoryEventStore,
    MongoEventStore,
    build_document,
    build_event_store,
    get_event_store,
    publish,
    set_event_store,
)
from app.models import MovementKind, Role
from app.services import record_movement


class BrokenEventStore:
    name = "broken"

    def append(self, documents):
        raise RuntimeError("event store is down")

    def query(self, criteria, limit):
        raise RuntimeError("event store is down")

    def ping(self):
        return False


def document(action: str, target_id: str, payload: dict, role: str = "admin") -> dict:
    return build_document(str(uuid.uuid4()), "actor-1", role, "Actor", action, "purchase", target_id, "", payload)


def test_commit_publishes_one_event_with_a_structured_payload(db, admin, item, store):
    record_movement(db, admin, item.id, MovementKind.outbound, 3, "Line 2", "")

    events = store.query({}, 10)
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "issued_stock"
    assert event["actor"]["role"] == Role.admin.value
    assert event["target"] == {"type": "item", "id": str(item.id)}
    # The payload is the point: none of these exist as columns on audit_logs.
    assert event["payload"]["quantity"] == 3
    assert event["payload"]["recipient"] == "Line 2"
    assert event["payload"]["quantity_on_hand_after"] == item.quantity_on_hand


def test_rollback_publishes_nothing(db, admin, item, store):
    """A rejected write must not leave an event claiming it happened."""
    with pytest.raises(HTTPException):
        record_movement(db, admin, item.id, MovementKind.outbound, item.quantity_on_hand + 1, "Line 2", "")
    db.rollback()

    assert store.query({}, 10) == []


def test_a_broken_store_does_not_fail_the_business_write(db, admin, item):
    set_event_store(BrokenEventStore())
    try:
        before = item.quantity_on_hand
        movement = record_movement(db, admin, item.id, MovementKind.outbound, 2, "Line 2", "")
        assert movement.quantity == 2
        db.refresh(item)
        assert item.quantity_on_hand == before - 2
    finally:
        set_event_store(None)


def test_payload_filters_select_on_fields_only_some_actions_carry(store):
    store.append([
        document("received_purchase", "p1", {"price_change_percent": 4.0}),
        document("received_purchase", "p2", {"price_change_percent": 31.5}),
        document("issued_stock", "i1", {"quantity": 5}),
    ])

    spikes = store.query({"payload.price_change_percent": {"$gte": 20}}, 10)
    assert [event["target"]["id"] for event in spikes] == ["p2"]

    # An event whose action has no such field is excluded, not read as zero.
    priced = store.query({"payload.price_change_percent": {"$gte": 0}}, 10)
    assert {event["target"]["id"] for event in priced} == {"p1", "p2"}


def test_criteria_combine_and_roles_filter(store):
    store.append([
        document("received_purchase", "p1", {"price_change_percent": 31.5}, role="admin"),
        document("received_purchase", "p2", {"price_change_percent": 31.5}, role="supervisor"),
    ])

    matched = store.query({"action": "received_purchase", "actor.role": "supervisor"}, 10)
    assert [event["target"]["id"] for event in matched] == ["p2"]


def test_query_returns_newest_first_and_honours_the_limit(store):
    store.append([document("issued_stock", f"i{index}", {"quantity": index}) for index in range(5)])

    events = store.query({}, 2)
    assert len(events) == 2
    assert events[0]["created_at"] >= events[1]["created_at"]


def test_in_memory_store_is_bounded(store):
    small = InMemoryEventStore(limit=3)
    small.append([document("issued_stock", f"i{index}", {"quantity": index}) for index in range(10)])

    assert len(small.query({}, 100)) == 3


def test_no_mongo_url_configured_uses_the_in_memory_store():
    assert isinstance(build_event_store("", "stockroom"), InMemoryEventStore)


def test_an_unreachable_mongo_degrades_instead_of_raising():
    """The degradation promise: a dead event store must not stop the API from
    starting, it only costs the richer query surface."""
    store = build_event_store("mongodb://127.0.0.1:1/?directConnection=true", "stockroom")

    assert isinstance(store, InMemoryEventStore)
    assert store.name == "memory"


def test_get_event_store_caches_and_set_event_store_replaces():
    set_event_store(None)
    try:
        first = get_event_store()
        assert first is get_event_store()

        replacement = InMemoryEventStore()
        set_event_store(replacement)
        assert get_event_store() is replacement
    finally:
        set_event_store(None)


def test_publish_is_a_no_op_for_an_empty_batch(store):
    assert publish([]) == 0


def test_publish_swallows_a_failing_store(store):
    set_event_store(BrokenEventStore())
    try:
        assert publish([document("issued_stock", "i1", {"quantity": 1})]) == 0
    finally:
        set_event_store(store)


def test_upper_bound_and_missing_fields_in_filters(store):
    store.append([
        document("received_purchase", "p1", {"price_change_percent": 4.0}),
        document("received_purchase", "p2", {"price_change_percent": 31.5}),
        document("issued_stock", "i1", {"quantity": 5}),
    ])

    cheap = store.query({"payload.price_change_percent": {"$lte": 10}}, 10)
    assert [event["target"]["id"] for event in cheap] == ["p1"]

    # A field no document has matches nothing rather than erroring.
    assert store.query({"payload.nonexistent": {"$gte": 1}}, 10) == []
    assert store.query({"payload.nonexistent": "anything"}, 10) == []


class FakeCollection:
    """Records what MongoEventStore asks of pymongo. The real-MongoDB test
    below covers query semantics; this one covers the calls being well-formed
    without needing a server."""

    def __init__(self):
        self.inserted = None
        self.ordered = None
        self.criteria = None
        self.sorted_by = None
        self.limited_to = None

    def insert_many(self, documents, ordered=True):
        self.inserted = documents
        self.ordered = ordered

    def find(self, criteria):
        self.criteria = criteria
        return self

    def sort(self, field, direction):
        self.sorted_by = (field, direction)
        return self

    def limit(self, count):
        self.limited_to = count
        return iter([{"_id": "x"}])


def test_mongo_store_inserts_unordered_and_queries_newest_first():
    collection = FakeCollection()
    store = MongoEventStore(collection)
    documents = [document("issued_stock", "i1", {"quantity": 1})]

    assert store.append(documents) == 1
    assert collection.inserted == documents
    # Unordered, so one duplicate _id from a retried flush cannot drop the rest.
    assert collection.ordered is False

    assert store.query({"action": "issued_stock"}, 25) == [{"_id": "x"}]
    assert collection.criteria == {"action": "issued_stock"}
    assert collection.sorted_by == ("created_at", -1)
    assert collection.limited_to == 25


@pytest.mark.skipif(not os.environ.get("MONGO_TEST_URL"), reason="MONGO_TEST_URL is not set; this needs a real MongoDB")
def test_against_a_real_mongodb():
    """The in-memory store only approximates Mongo's query semantics, so the
    same filters run against the real thing to stop the two diverging."""
    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGO_TEST_URL"], serverSelectionTimeoutMS=3000)
    database = client["stockroom_test"]
    database.drop_collection("audit_events")
    store = MongoEventStore(database["audit_events"])

    store.append([
        document("received_purchase", "p1", {"price_change_percent": 4.0}),
        document("received_purchase", "p2", {"price_change_percent": 31.5}),
        document("issued_stock", "i1", {"quantity": 5}, role="supervisor"),
    ])

    assert [event["target"]["id"] for event in store.query({"payload.price_change_percent": {"$gte": 20}}, 10)] == ["p2"]
    assert len(store.query({"actor.role": "admin"}, 10)) == 2
    assert store.query({"action": "issued_stock", "payload.quantity": {"$gte": 10}}, 10) == []
    assert store.ping() is True

    database.drop_collection("audit_events")
    client.close()
