"""TTL caching and stale-on-error semantics for market data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class PersistentCache(Protocol):
    async def get_market_cache(self, cache_key: str) -> tuple[object, datetime] | None: ...

    async def set_market_cache(
        self,
        cache_key: str,
        payload: object,
        expires_at: datetime,
        *,
        provider: str,
    ) -> None: ...


@dataclass(slots=True)
class CacheValue(Generic[T]):
    value: T
    expires_at: datetime
    cached_at: datetime


class SingleFlightTTLCache:
    """Small in-process cache preventing a request stampede per cache key."""

    def __init__(self) -> None:
        self._values: dict[str, CacheValue[object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl: timedelta,
        max_stale: timedelta | None = None,
        mark_stale: Callable[[T], T] | None = None,
    ) -> T:
        now = datetime.now(timezone.utc)
        cached = self._values.get(key)
        if cached and cached.expires_at > now:
            return cached.value  # type: ignore[return-value]

        lock = await self._lock_for(key)
        async with lock:
            now = datetime.now(timezone.utc)
            cached = self._values.get(key)
            if cached and cached.expires_at > now:
                return cached.value  # type: ignore[return-value]
            try:
                loaded = await loader()
            except Exception:
                if cached and max_stale is not None and cached.cached_at + max_stale >= now:
                    stale_value = cached.value  # type: ignore[assignment]
                    return mark_stale(stale_value) if mark_stale else stale_value  # type: ignore[return-value]
                raise
            self._values[key] = CacheValue(
                value=loaded,
                expires_at=now + ttl,
                cached_at=now,
            )
            return loaded

    def clear(self, prefix: str | None = None) -> None:
        if prefix is None:
            self._values.clear()
            return
        for key in tuple(self._values):
            if key.startswith(prefix):
                del self._values[key]
