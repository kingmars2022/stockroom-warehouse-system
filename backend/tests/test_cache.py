"""Cache behaviour: hits, misses, expiry, invalidation, and graceful degradation.

These tests pin the contract the API depends on — a cache failure must never
turn into a request failure, and a write must never leave stale replenishment
guidance readable.
"""

import json
import time

import pytest

from app.cache import (
    REPLENISHMENT_KEY,
    REPLENISHMENT_PREFIX,
    Cache,
    CacheStats,
    InMemoryBackend,
    RedisBackend,
    build_backend,
    get_cache,
    set_cache,
)
from app.models import Item, MovementKind, Purchase, Supplier, SupplierStatus
from app.services import (
    cached_replenishment_recommendations,
    invalidate_replenishment_cache,
    record_movement,
    record_purchase,
    replenishment_recommendations,
)


# --------------------------------------------------------------------------
# Backend: in-memory
# --------------------------------------------------------------------------

def test_memory_backend_round_trips_a_value():
    backend = InMemoryBackend()
    backend.set("k", "v", 60)
    assert backend.get("k") == "v"


def test_memory_backend_returns_none_for_missing_key():
    assert InMemoryBackend().get("nope") is None


def test_memory_backend_expires_entries():
    backend = InMemoryBackend()
    backend.set("k", "v", 1)
    assert backend.get("k") == "v"
    time.sleep(1.05)
    assert backend.get("k") is None


def test_memory_backend_zero_ttl_never_expires():
    backend = InMemoryBackend()
    backend.set("k", "v", 0)
    time.sleep(0.05)
    assert backend.get("k") == "v"


def test_memory_backend_delete_prefix_only_removes_matching_keys():
    backend = InMemoryBackend()
    backend.set("a:1", "x", 60)
    backend.set("a:2", "y", 60)
    backend.set("b:1", "z", 60)

    removed = backend.delete_prefix("a:")

    assert removed == 2
    assert backend.get("a:1") is None
    assert backend.get("b:1") == "z"


def test_memory_backend_ping_is_true():
    assert InMemoryBackend().ping() is True


# --------------------------------------------------------------------------
# Cache semantics
# --------------------------------------------------------------------------

def test_first_call_is_a_miss_and_second_is_a_hit():
    cache = Cache(InMemoryBackend(), ttl_seconds=60)
    calls = []

    def producer():
        calls.append(1)
        return {"value": 42}

    assert cache.get_or_set("k", producer) == {"value": 42}
    assert cache.get_or_set("k", producer) == {"value": 42}

    assert len(calls) == 1
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1


def test_invalidate_forces_recomputation():
    cache = Cache(InMemoryBackend(), ttl_seconds=60)
    calls = []
    producer = lambda: (calls.append(1), {"n": len(calls)})[1]  # noqa: E731

    cache.get_or_set("k", producer)
    cache.invalidate("k")
    cache.get_or_set("k", producer)

    assert len(calls) == 2
    assert cache.stats.invalidations == 1


def test_expired_entry_is_recomputed():
    cache = Cache(InMemoryBackend(), ttl_seconds=1)
    calls = []
    producer = lambda: (calls.append(1), "v")[1]  # noqa: E731

    cache.get_or_set("k", producer)
    time.sleep(1.05)
    cache.get_or_set("k", producer)

    assert len(calls) == 2


def test_corrupt_entry_is_discarded_and_counted():
    backend = InMemoryBackend()
    backend.set("k", "{not json", 60)
    cache = Cache(backend, ttl_seconds=60)

    assert cache.get_or_set("k", lambda: "fresh") == "fresh"
    assert cache.stats.errors == 1
    assert cache.stats.misses == 1


def test_read_failure_degrades_to_recomputation():
    class ExplodingRead(InMemoryBackend):
        def get(self, key):
            raise RuntimeError("redis is down")

    cache = Cache(ExplodingRead(), ttl_seconds=60)

    assert cache.get_or_set("k", lambda: "fresh") == "fresh"
    assert cache.stats.errors == 1


def test_write_failure_still_returns_the_value():
    class ExplodingWrite(InMemoryBackend):
        def set(self, key, value, ttl_seconds):
            raise RuntimeError("redis is down")

    cache = Cache(ExplodingWrite(), ttl_seconds=60)

    assert cache.get_or_set("k", lambda: "fresh") == "fresh"
    assert cache.stats.errors == 1


def test_invalidation_failure_is_swallowed():
    class ExplodingDelete(InMemoryBackend):
        def delete_prefix(self, prefix):
            raise RuntimeError("redis is down")

    cache = Cache(ExplodingDelete(), ttl_seconds=60)

    assert cache.invalidate("k") == 0
    assert cache.stats.errors == 1


def test_stats_report_hit_rate():
    stats = CacheStats()
    stats.hits, stats.misses = 3, 1
    assert stats.as_dict()["hit_rate_percent"] == 75.0


def test_stats_hit_rate_is_zero_when_unused():
    assert CacheStats().as_dict()["hit_rate_percent"] == 0


def test_stats_reset_clears_counters():
    stats = CacheStats(hits=5, misses=2, invalidations=1, errors=3)
    stats.reset()
    assert stats.as_dict()["hits"] == 0
    assert stats.as_dict()["errors"] == 0


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

def test_build_backend_without_url_uses_memory():
    assert isinstance(build_backend(""), InMemoryBackend)


def test_build_backend_falls_back_when_redis_is_unreachable():
    # Port 1 is reserved and never listening, so the connect times out.
    assert isinstance(build_backend("redis://127.0.0.1:1/0"), InMemoryBackend)


def test_get_cache_returns_a_singleton():
    set_cache(None)
    first, second = get_cache(), get_cache()
    assert first is second


# --------------------------------------------------------------------------
# Redis backend, exercised against fakeredis
# --------------------------------------------------------------------------

@pytest.fixture()
def redis_cache():
    fakeredis = pytest.importorskip("fakeredis")
    return Cache(RedisBackend(fakeredis.FakeRedis()), ttl_seconds=60)


def test_redis_backend_round_trips_json(redis_cache):
    assert redis_cache.get_or_set("k", lambda: {"a": 1}) == {"a": 1}
    assert redis_cache.get_or_set("k", lambda: {"a": 2}) == {"a": 1}
    assert redis_cache.stats.hits == 1


def test_redis_backend_scan_delete_removes_prefixed_keys(redis_cache):
    redis_cache.get_or_set("stockroom:x:1", lambda: 1)
    redis_cache.get_or_set("stockroom:x:2", lambda: 2)
    redis_cache.get_or_set("other:1", lambda: 3)

    assert redis_cache.invalidate("stockroom:x:") == 2
    assert redis_cache.backend.get("other:1") == json.dumps(3)


def test_redis_backend_ping(redis_cache):
    assert redis_cache.backend.ping() is True


def test_redis_backend_reports_its_name(redis_cache):
    assert redis_cache.backend_name == "redis"


# --------------------------------------------------------------------------
# Integration: replenishment guidance is cached and invalidated on write
# --------------------------------------------------------------------------

def _stocked_item(db, supervisor, quantity_on_hand=4):
    item = Item(sku="LBL-1", name="Labels", quantity_on_hand=quantity_on_hand, minimum_quantity=10, unit="rolls")
    supplier = Supplier(name="Label Co", lead_days=3, rating=4.0, status=SupplierStatus.preferred)
    db.add_all([item, supplier])
    db.commit()
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=supervisor.id, quantity=20, unit_cost=9, currency="USD", invoice_number="P-1", price_change_percent=0))
    db.commit()
    return item, supplier


def test_replenishment_second_read_is_served_from_cache(db, supervisor, cache):
    _stocked_item(db, supervisor)

    cached_replenishment_recommendations(db)
    cached_replenishment_recommendations(db)

    assert cache.stats.misses == 1
    assert cache.stats.hits == 1


def test_cached_replenishment_matches_the_uncached_result(db, supervisor):
    item, _ = _stocked_item(db, supervisor)

    direct = replenishment_recommendations(db)
    cached = cached_replenishment_recommendations(db)

    assert len(cached) == len(direct)
    assert cached[0]["item_id"] == str(item.id)  # JSON round trip stringifies UUIDs
    assert cached[0]["suggested_quantity"] == direct[0]["suggested_quantity"]


def test_stock_movement_invalidates_the_cached_recommendation(db, supervisor, employee, cache):
    item, _ = _stocked_item(db, supervisor, quantity_on_hand=40)

    before = cached_replenishment_recommendations(db)
    record_movement(db, employee, item.id, MovementKind.outbound, 35, "Dispatch", "Bulk run")
    after = cached_replenishment_recommendations(db)

    assert cache.stats.invalidations >= 1
    assert before != after


def test_purchase_invalidates_the_cached_recommendation(db, supervisor, cache):
    item, supplier = _stocked_item(db, supervisor)
    cached_replenishment_recommendations(db)

    record_purchase(db, supervisor, item.id, supplier.id, 100, 9.5, "USD", "P-2", None)

    assert cache.stats.invalidations >= 1
    assert cache.backend.get(REPLENISHMENT_KEY) is None


def test_invalidate_helper_targets_the_replenishment_prefix(db, supervisor, cache):
    _stocked_item(db, supervisor)
    cached_replenishment_recommendations(db)
    assert cache.backend.get(REPLENISHMENT_KEY) is not None

    removed = invalidate_replenishment_cache()

    assert removed == 1
    assert REPLENISHMENT_KEY.startswith(REPLENISHMENT_PREFIX)
    assert cache.backend.get(REPLENISHMENT_KEY) is None
