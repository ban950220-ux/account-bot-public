"""TTL and last-good fallback wrapper for any market-data provider."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from .base import (
    DistributionSeries,
    EarningsData,
    FundamentalData,
    FxQuote,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    TotalReturnSeries,
    TrendData,
    ValuationData,
)
from .cache import SingleFlightTTLCache


class CachedMarketDataProvider(MarketDataProvider):
    """Apply conservative, per-dataset cache windows to a provider.

    Holdings and executions never pass through this wrapper.  It is only for
    public enrichment data.  A provider error may return a bounded last-good
    value; prices and FX quotes are explicitly marked stale.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        cache: SingleFlightTTLCache | None = None,
        price_ttl: timedelta = timedelta(minutes=10),
        fx_ttl: timedelta = timedelta(minutes=30),
        fundamental_ttl: timedelta = timedelta(days=1),
        valuation_ttl: timedelta = timedelta(days=7),
        earnings_ttl: timedelta = timedelta(days=1),
        news_ttl: timedelta = timedelta(hours=3),
        trend_ttl: timedelta = timedelta(days=1),
        max_stale: timedelta = timedelta(days=3),
    ) -> None:
        self.provider = provider
        self.name = f"cached:{provider.name}"
        self.cache = cache or SingleFlightTTLCache()
        self.price_ttl = price_ttl
        self.fx_ttl = fx_ttl
        self.fundamental_ttl = fundamental_ttl
        self.valuation_ttl = valuation_ttl
        self.earnings_ttl = earnings_ttl
        self.news_ttl = news_ttl
        self.trend_ttl = trend_ttl
        self.max_stale = max_stale

    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        return await self.cache.get_or_load(
            f"price:{market.upper()}:{ticker.upper()}",
            lambda: self.provider.get_price(market, ticker),
            ttl=self.price_ttl,
            max_stale=self.max_stale,
            mark_stale=lambda value: replace(value, stale=True),
        )

    async def get_fx(self, base: str, quote: str) -> FxQuote:
        return await self.cache.get_or_load(
            f"fx:{base.upper()}:{quote.upper()}",
            lambda: self.provider.get_fx(base, quote),
            ttl=self.fx_ttl,
            max_stale=self.max_stale,
            mark_stale=lambda value: replace(value, stale=True),
        )

    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:
        return await self.cache.get_or_load(
            f"fundamental:{market.upper()}:{ticker.upper()}",
            lambda: self.provider.get_fundamentals(market, ticker),
            ttl=self.fundamental_ttl,
            max_stale=self.max_stale,
        )

    async def get_valuation(self, market: str, ticker: str) -> ValuationData:
        return await self.cache.get_or_load(
            f"valuation:{market.upper()}:{ticker.upper()}",
            lambda: self.provider.get_valuation(market, ticker),
            ttl=self.valuation_ttl,
            max_stale=self.max_stale,
        )

    async def get_earnings(self, market: str, ticker: str) -> EarningsData:
        return await self.cache.get_or_load(
            f"earnings:{market.upper()}:{ticker.upper()}",
            lambda: self.provider.get_earnings(market, ticker),
            ttl=self.earnings_ttl,
            max_stale=self.max_stale,
        )

    async def get_news(self, market: str, ticker: str, *, limit: int = 10) -> list[NewsItem]:
        cached = await self.cache.get_or_load(
            f"news:{market.upper()}:{ticker.upper()}:{limit}",
            lambda: self.provider.get_news(market, ticker, limit=limit),
            ttl=self.news_ttl,
            max_stale=self.max_stale,
        )
        # Do not expose the mutable cache value to callers.
        return list(cached)

    async def get_trend(self, market: str, ticker: str) -> TrendData:
        return await self.cache.get_or_load(
            f"trend:{market.upper()}:{ticker.upper()}",
            lambda: self.provider.get_trend(market, ticker),
            ttl=self.trend_ttl,
            max_stale=self.max_stale,
        )

    async def get_total_return_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 10,
    ) -> TotalReturnSeries:
        return await self.cache.get_or_load(
            f"total-return:{market.upper()}:{ticker.upper()}:{lookback_years}",
            lambda: self.provider.get_total_return_series(
                market, ticker, lookback_years=lookback_years
            ),
            ttl=self.trend_ttl,
            max_stale=self.max_stale,
            mark_stale=lambda value: replace(value, stale=True),
        )

    async def get_distribution_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 5,
    ) -> DistributionSeries:
        return await self.cache.get_or_load(
            f"distribution:{market.upper()}:{ticker.upper()}:{lookback_years}",
            lambda: self.provider.get_distribution_series(
                market, ticker, lookback_years=lookback_years
            ),
            ttl=self.trend_ttl,
            max_stale=self.max_stale,
            mark_stale=lambda value: replace(value, stale=True),
        )
