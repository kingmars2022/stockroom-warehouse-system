"""Queryable audit events, stored as documents alongside the relational log.

PostgreSQL stays the system of record: `audit_logs` is written inside the same
transaction as the business change, so an audit row cannot go missing. What it
cannot do is answer questions *about* a change, because the interesting values
are flattened into one `detail` sentence.

Each action carries a different shape — an issue has a recipient and a
quantity, a purchase has a unit cost and a price delta, a reimbursement has an
amount and a status transition. Modelling that relationally means either a
column per action or a key/value side table. Documents fit it directly, and
Mongo can index into the payload, so "every purchase that rose more than 20%"
becomes a query instead of a scan.

Two properties matter and are enforced here:

* **No orphans.** Events are buffered on the session and flushed by an
  `after_commit` hook, so a rolled-back transaction publishes nothing.
* **Never blocks a write.** If Mongo is unreachable the store degrades to a
  bounded in-process buffer, exactly as the replenishment cache does. Audit
  querying gets worse; issuing stock does not start failing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

COLLECTION = "audit_events"
IN_MEMORY_LIMIT = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventStoreBackend(Protocol):
    name: str

    def append(self, documents: list[dict[str, Any]]) -> int: ...
    def query(self, criteria: dict[str, Any], limit: int) -> list[dict[str, Any]]: ...
    def ping(self) -> bool: ...


def _matches(document: dict[str, Any], criteria: dict[str, Any]) -> bool:
    """Evaluate the small subset of Mongo query syntax this module emits."""
    for field, expected in criteria.items():
        actual: Any = document
        for part in field.split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if actual is None:
                    return False
                if operator == "$gte" and not actual >= operand:
                    return False
                if operator == "$lte" and not actual <= operand:
                    return False
        elif actual != expected:
            return False
    return True


class InMemoryEventStore:
    """Fallback used when Mongo is unreachable. Bounded, so it cannot grow unbounded."""

    name = "memory"

    def __init__(self, limit: int = IN_MEMORY_LIMIT) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def append(self, documents: list[dict[str, Any]]) -> int:
        with self._lock:
            self._events.extend(documents)
        return len(documents)

    def query(self, criteria: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        matched = [event for event in events if _matches(event, criteria)]
        matched.sort(key=lambda event: event["created_at"], reverse=True)
        return matched[:limit]

    def ping(self) -> bool:
        return True


class MongoEventStore:
    name = "mongodb"

    def __init__(self, collection: Any) -> None:
        self._collection = collection

    def append(self, documents: list[dict[str, Any]]) -> int:
        # Ordered=False so one duplicate _id (a retried flush) cannot discard
        # the rest of the batch.
        self._collection.insert_many(documents, ordered=False)
        return len(documents)

    def query(self, criteria: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        cursor = self._collection.find(criteria).sort("created_at", -1).limit(limit)
        return list(cursor)

    def ping(self) -> bool:
        self._collection.database.client.admin.command("ping")
        return True


def build_event_store(mongo_url: str, database: str) -> EventStoreBackend:
    """Return a Mongo-backed store, or the in-memory fallback if that is not reachable."""
    if not mongo_url:
        return InMemoryEventStore()
    try:
        from pymongo import ASCENDING, DESCENDING, MongoClient

        client = MongoClient(mongo_url, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        collection = client[database][COLLECTION]
        # Supports the endpoint's filters: newest-first listing, and reaching
        # into the payload without a collection scan.
        collection.create_index([("created_at", DESCENDING)])
        collection.create_index([("action", ASCENDING), ("created_at", DESCENDING)])
        collection.create_index([("payload.price_change_percent", DESCENDING)], sparse=True)
        return MongoEventStore(collection)
    except Exception:
        logger.warning("MongoDB unavailable, audit events fall back to memory", exc_info=True)
        return InMemoryEventStore()


_store: EventStoreBackend | None = None
_store_lock = threading.Lock()


def get_event_store() -> EventStoreBackend:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from .config import get_settings

                settings = get_settings()
                _store = build_event_store(settings.mongo_url, settings.mongo_database)
    return _store


def set_event_store(store: EventStoreBackend | None) -> None:
    """Swap the store. Tests use this; nothing in the request path calls it."""
    global _store
    _store = store


def build_document(
    event_id: str,
    actor_id: str,
    actor_role: str,
    actor_name: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Shape one event. `_id` is the relational row's id, so the two stores join
    and a replayed flush is idempotent rather than duplicating."""
    return {
        "_id": event_id,
        "actor": {"id": actor_id, "role": actor_role, "name": actor_name},
        "action": action,
        "target": {"type": target_type, "id": target_id},
        "detail": detail,
        "payload": payload or {},
        "created_at": _utcnow(),
    }


def publish(documents: list[dict[str, Any]]) -> int:
    """Append, degrading to memory if the store fails at runtime.

    `build_event_store` only covers Mongo being unreachable at startup. A
    server that goes away afterwards is the more common case, and until this
    also handled it the documents were simply dropped — which is not what
    "degrades to an in-process buffer" describes. Falling back here keeps the
    events readable for the rest of the process's life.

    A business write is never failed either way.
    """
    if not documents:
        return 0
    store = get_event_store()
    try:
        return store.append(documents)
    except Exception:
        logger.warning("Event store %r failed, falling back to memory", store.name, exc_info=True)

    if isinstance(store, InMemoryEventStore):
        return 0
    fallback = InMemoryEventStore()
    set_event_store(fallback)
    try:
        return fallback.append(documents)
    except Exception:
        logger.warning("Could not publish %d audit events", len(documents), exc_info=True)
        return 0
