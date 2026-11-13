from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.market import (  # noqa: E402
    CachedMarketDataProvider,
    EarningsData,
    FundamentalData,
    FxQuote,
    MarketDataError,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    ValuationData,
)


class MutableClock:
    current = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


class FakeMarketProvider(MarketDataProvider):
    name = "fake-market"

    def __init__(self, *, block_price: bool = False) -> None:
        self.price_calls = 0
        self.fx_calls = 0
        self.fail = False
        self.block_price = block_price
        self.price_started = asyncio.Event()
        self.release_price = asyncio.Event()
        if not block_price:
            self.release_price.set()

    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        self.price_calls += 1
        self.price_started.set()
        await self.release_price.wait()
        if self.fail:
            raise MarketDataError("sanitized fixture failure")
        return PriceQuote(
            market=market.upper(),
            ticker=ticker.upper(),
            currency="USD",
            price=Decimal("168.20"),
            as_of=MutableClock.current,
            provider=self.name,
        )

    async def get_fx(self, base: str, quote: str) -> FxQuote:
        self.fx_calls += 1
        if self.fail:
            raise MarketDataError("sanitized fixture failure")
        return FxQuote(
            base=base.upper(),
            quote=quote.upper(),
            rate=Decimal("1380.25"),
            as_of=MutableClock.current,
            provider=self.name,
        )

    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:
        raise NotImplementedError

    async def get_valuation(self, market: str, ticker: str) -> ValuationData:
        raise NotImplementedError

    async def get_earnings(self, market: str, ticker: str) -> EarningsData:
        raise NotImplementedError

    async def get_news(self, market: str, ticker: str, *, limit: int = 10) -> list[NewsItem]:
        raise NotImplementedError


class CachedMarketDataProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        MutableClock.current = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)

    async def test_price_ttl_uses_single_flight_for_concurrent_callers(self) -> None:
        provider = FakeMarketProvider(block_price=True)
        cached = CachedMarketDataProvider(
            provider,
            price_ttl=timedelta(minutes=10),
        )
        with patch("account_bot.market.cache.datetime", MutableClock):
            tasks = [asyncio.create_task(cached.get_price("nasdaq", "amd")) for _ in range(8)]
            await provider.price_started.wait()
            # Give the remaining callers a chance to queue on the per-key lock.
            await asyncio.sleep(0)
            provider.release_price.set()
            results = await asyncio.gather(*tasks)

            self.assertEqual(provider.price_calls, 1)
            self.assertTrue(all(item.price == Decimal("168.20") for item in results))

            # Same normalized key remains hot within the TTL.
            again = await cached.get_price("NASDAQ", "AMD")
            self.assertEqual(again.price, Decimal("168.20"))
            self.assertEqual(provider.price_calls, 1)

            # Advancing the injected clock beyond TTL triggers exactly one reload.
            MutableClock.current += timedelta(minutes=11)
            await cached.get_price("NASDAQ", "AMD")
            self.assertEqual(provider.price_calls, 2)

    async def test_provider_failure_returns_bounded_last_good_price_and_fx_as_stale(self) -> None:
        provider = FakeMarketProvider()
        cached = CachedMarketDataProvider(
            provider,
            price_ttl=timedelta(minutes=10),
            fx_ttl=timedelta(minutes=10),
            max_stale=timedelta(days=1),
        )
        with patch("account_bot.market.cache.datetime", MutableClock):
            original_price = await cached.get_price("NASDAQ", "AMD")
            original_fx = await cached.get_fx("USD", "KRW")
            self.assertFalse(original_price.stale)
            self.assertFalse(original_fx.stale)

            MutableClock.current += timedelta(minutes=11)
            provider.fail = True
            stale_price = await cached.get_price("NASDAQ", "AMD")
            stale_fx = await cached.get_fx("USD", "KRW")

        self.assertEqual(provider.price_calls, 2)
        self.assertEqual(provider.fx_calls, 2)
        self.assertEqual(stale_price.price, original_price.price)
        self.assertEqual(stale_fx.rate, original_fx.rate)
        self.assertTrue(stale_price.stale)
        self.assertTrue(stale_fx.stale)

    async def test_failure_after_max_stale_does_not_hide_provider_error(self) -> None:
        provider = FakeMarketProvider()
        cached = CachedMarketDataProvider(
            provider,
            price_ttl=timedelta(minutes=10),
            max_stale=timedelta(hours=1),
        )
        with patch("account_bot.market.cache.datetime", MutableClock):
            await cached.get_price("NASDAQ", "AMD")
            MutableClock.current += timedelta(hours=2)
            provider.fail = True
            with self.assertRaises(MarketDataError):
                await cached.get_price("NASDAQ", "AMD")


if __name__ == "__main__":
    unittest.main()
