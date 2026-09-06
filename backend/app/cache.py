"""Read-through cache for expensive aggregate queries.

The replenishment endpoint scans every item, supplier, stock movement and
purchase, then ranks suppliers per item. That work is identical for every
caller until stock actually moves, so the result is cached and invalidated on
write instead of recomputed per request.

Backends
--------
`RedisBackend`     used whenever ``REDIS_URL`` is set and the server answers a
                   PING at startup. Shared across API workers, so a write in
                   one worker invalidates the entry every worker reads.
`InMemoryBackend`  process-local fallback for local development and tests, so
                   neither requires a running Redis. It is *not* shared across
                   workers and is deliberately not used when Redis is
                   configured.

The cache is never allowed to break a request: every backend call is guarded,
and any failure degrades to recomputing from the database while incrementing
``stats.errors``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

REPLENISHMENT_PREFIX = "stockroom:replenishment:"
REPLENISHMENT_KEY = f"{REPLENISHMENT_PREFIX}v1"


@dataclass
class CacheStats:
    """Counters exposed on /health so cache behaviour is observable in prod."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    errors: int = 0

    def reset(self) -> None:
        self.hits = self.misses = self.invalidations = self.errors = 0

    def as_dict(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "errors": self.errors,
            "hit_rate_percent": round(self.hits / total * 100, 1) if total else 0.0,
        }


class CacheBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def ping(self) -> bool: ...


class InMemoryBackend:
    """Thread-safe dict with per-key expiry. Dev and test only."""

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, payload = entry
            if expires_at is not None and expires_at <= time.monotonic():
                self._data.pop(key, None)
                return None
            return payload

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds > 0 else None
        with self._lock:
            self._data[key] = (expires_at, value)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            doomed = [key for key in self._data if key.startswith(prefix)]
            for key in doomed:
                self._data.pop(key, None)
            return len(doomed)

    def ping(self) -> bool:
        return True


class RedisBackend:
    """Redis-backed store. SCAN is used for prefix deletes so a large keyspace
    is never blocked by KEYS."""

    name = "redis"

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, key: str) -> str | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            self._client.setex(key, ttl_seconds, value)
        else:
            self._client.set(key, value)

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        for key in self._client.scan_iter(match=f"{prefix}*", count=500):
            removed += int(self._client.delete(key) or 0)
        return removed

    def ping(self) -> bool:
        return bool(self._client.ping())


class Cache:
    """Read-through cache with graceful degradation."""

    def __init__(self, backend: CacheBackend, ttl_seconds: int) -> None:
        self.backend = backend
        self.ttl_seconds = ttl_seconds
        self.stats = CacheStats()

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", "unknown")

    def get_or_set(self, key: str, producer: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``; compute and store it on a miss.

        The returned value has the same shape on a hit and on a miss: both come
        back through JSON, so a caller can never depend on a type that only
        survives while the cache is cold.

        A backend failure is logged and counted, then treated as a miss so the
        caller still gets a correct (uncached) answer.
        """
        raw: str | None = None
        try:
            raw = self.backend.get(key)
        except Exception:  # noqa: BLE001 - cache must never fail the request
            self.stats.errors += 1
            logger.warning("cache read failed for %s", key, exc_info=True)

        if raw is not None:
            try:
                value = json.loads(raw)
                self.stats.hits += 1
                return value
            except (json.JSONDecodeError, TypeError):
                self.stats.errors += 1
                logger.warning("discarding corrupt cache entry for %s", key)

        self.stats.misses += 1
        value = producer()

        try:
            payload = json.dumps(value, default=str)
        except (TypeError, ValueError):
            self.stats.errors += 1
            logger.warning("value for %s is not JSON-serialisable; not caching", key)
            return value

        try:
            self.backend.set(key, payload, self.ttl_seconds)
        except Exception:  # noqa: BLE001
            self.stats.errors += 1
            logger.warning("cache write failed for %s", key, exc_info=True)

        # Return the round-tripped value so callers see the same shape whether
        # this was a hit or a miss. Returning the producer's object directly
        # would let `default=str` change types (UUID -> str) only once an entry
        # is cached - a bug that hides in a cold test run and appears in
        # production the moment the cache warms up.
        return json.loads(payload)

    def invalidate(self, prefix: str) -> int:
        try:
            removed = self.backend.delete_prefix(prefix)
        except Exception:  # noqa: BLE001
            self.stats.errors += 1
            logger.warning("cache invalidation failed for %s", prefix, exc_info=True)
            return 0
        self.stats.invalidations += removed
        return removed


def build_backend(redis_url: str) -> CacheBackend:
    """Return a Redis backend when one is configured and reachable, else memory.

    Falling back rather than raising keeps the API bootable when Redis is down;
    the backend in use is reported by /health.
    """
    if not redis_url:
        return InMemoryBackend()
    try:
        import redis  # imported lazily so the package stays optional in dev

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        backend = RedisBackend(client)
        backend.ping()
        logger.info("cache backend: redis (%s)", redis_url)
        return backend
    except Exception:  # noqa: BLE001
        logger.warning("redis unavailable at %s, falling back to in-memory cache", redis_url, exc_info=True)
        return InMemoryBackend()


_cache: Cache | None = None
_cache_lock = threading.Lock()


def get_cache() -> Cache:
    """Process-wide cache singleton, built on first use."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                from .config import get_settings

                settings = get_settings()
                _cache = Cache(build_backend(settings.redis_url), settings.cache_ttl_seconds)
    return _cache


def set_cache(cache: Cache | None) -> None:
    """Replace the singleton. Used by tests to inject a fresh backend."""
    global _cache
    with _cache_lock:
        _cache = cache
