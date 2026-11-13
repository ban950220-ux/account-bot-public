"""Public-data enrichment for the local dashboard with persisted last-good fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from account_bot.market import MarketDataError, MarketDataProvider
from account_bot.services.storage_adapters import SqlAlchemyFxRateStore
from account_bot.storage import SQLitePortfolioRepository


@dataclass(frozen=True, slots=True)
class WebPrice:
    ticker: str
    price: Decimal
    as_of: datetime
    provider: str
    stale: bool


@dataclass(frozen=True, slots=True)
class WebFx:
    rate: Decimal
    as_of: datetime
    provider: str
    stale: bool


@dataclass(frozen=True, slots=True)
class WebTotalReturnSeries:
    ticker: str
    currency: str
    as_of: datetime
    provider: str
    series_kind: str
    points: tuple[tuple[date, Decimal], ...]
    stale: bool


@dataclass(frozen=True, slots=True)
class WebDistributionSeries:
    ticker: str
    currency: str
    as_of: datetime
    provider: str
    coverage_start: date
    coverage_end: date
    points: tuple[tuple[date, Decimal, str], ...]
    stale: bool


class WebMarketDataService:
    """Keep external calls optional and persist only normalized public data."""

    def __init__(
        self,
        repository: SQLitePortfolioRepository,
        provider: MarketDataProvider | None,
        *,
        max_concurrency: int = 5,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._fx_store = SqlAlchemyFxRateStore(repository)

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    async def get_fx(self, *, refresh: bool = False) -> WebFx | None:
        provider_name = self.provider.name if self.provider else "yfinance"
        cached = await self.repository.get_market_cache(
            provider=provider_name,
            namespace="WEB_FX",
            cache_key="USD:KRW",
            allow_stale=refresh or self.provider is None,
        )
        if cached is not None and not refresh and not cached.is_stale:
            return WebFx(
                rate=Decimal(str(cached.data["rate"])),
                as_of=datetime.fromisoformat(str(cached.data["asOf"])),
                provider=cached.provider,
                stale=cached.is_stale,
            )
        if self.provider is None:
            return self._cached_fx(cached) or await self._last_good_fx()
        try:
            quote = await self.provider.get_fx("USD", "KRW")
            now = datetime.now(timezone.utc)
            await self.repository.put_market_cache(
                provider=provider_name,
                namespace="WEB_FX",
                cache_key="USD:KRW",
                normalized_data={"rate": str(quote.rate), "asOf": quote.as_of.isoformat()},
                fetched_at=now,
                expires_at=now + timedelta(minutes=10),
                stale_until=now + timedelta(days=7),
            )
            await self._fx_store.save_fx_rate(
                base="USD",
                quote="KRW",
                rate=quote.rate,
                observed_at=quote.as_of,
                provider=quote.provider,
            )
            return WebFx(quote.rate, quote.as_of, quote.provider, quote.stale)
        except MarketDataError:
            fallback = await self.repository.get_market_cache(
                provider=provider_name,
                namespace="WEB_FX",
                cache_key="USD:KRW",
                allow_stale=True,
            )
            return self._cached_fx(fallback, force_stale=True) or await self._last_good_fx()

    async def _last_good_fx(self) -> WebFx | None:
        value = await self._fx_store.get_last_fx_rate(
            base="USD",
            quote="KRW",
            not_older_than=datetime.now(timezone.utc) - timedelta(days=7),
        )
        if value is None:
            return None
        rate, observed_at, provider = value
        return WebFx(rate, observed_at, provider, True)

    @staticmethod
    def _cached_fx(cached, *, force_stale: bool = False) -> WebFx | None:
        if cached is None:
            return None
        return WebFx(
            Decimal(str(cached.data["rate"])),
            datetime.fromisoformat(str(cached.data["asOf"])),
            cached.provider,
            cached.is_stale or force_stale,
        )

    async def get_prices(
        self, tickers: Iterable[str], *, refresh: bool = False
    ) -> dict[str, WebPrice]:
        pairs = await asyncio.gather(
            *(self._get_price(ticker, refresh=refresh) for ticker in sorted(set(tickers)))
        )
        return {item.ticker: item for item in pairs if item is not None}

    async def _get_price(self, ticker: str, *, refresh: bool) -> WebPrice | None:
        provider_name = self.provider.name if self.provider else "yfinance"
        cache_key = f"US:{ticker}"
        cached = await self.repository.get_market_cache(
            provider=provider_name,
            namespace="WEB_PRICE",
            cache_key=cache_key,
            allow_stale=refresh or self.provider is None,
        )
        if cached is not None and not refresh and not cached.is_stale:
            return self._cached_price(ticker, cached)
        if self.provider is None:
            return self._cached_price(ticker, cached)
        try:
            async with self._semaphore:
                quote = await self.provider.get_price("US", ticker)
            if quote.currency != "USD":
                return None
            now = datetime.now(timezone.utc)
            await self.repository.put_market_cache(
                provider=provider_name,
                namespace="WEB_PRICE",
                cache_key=cache_key,
                normalized_data={"price": str(quote.price), "asOf": quote.as_of.isoformat()},
                fetched_at=now,
                expires_at=now + timedelta(minutes=10),
                stale_until=now + timedelta(days=3),
            )
            await self.repository.add_market_price(
                market="US",
                ticker=ticker,
                asset_type="ETF",
                currency="USD",
                price=quote.price,
                as_of=quote.as_of,
                provider=quote.provider,
                source_kind="LIVE",
                stale=quote.stale,
                fetched_at=now,
            )
            return WebPrice(ticker, quote.price, quote.as_of, quote.provider, quote.stale)
        except MarketDataError:
            fallback = await self.repository.get_market_cache(
                provider=provider_name,
                namespace="WEB_PRICE",
                cache_key=cache_key,
                allow_stale=True,
            )
            return self._cached_price(ticker, fallback, force_stale=True)

    @staticmethod
    def _cached_price(ticker: str, cached, *, force_stale: bool = False) -> WebPrice | None:
        if cached is None:
            return None
        return WebPrice(
            ticker,
            Decimal(str(cached.data["price"])),
            datetime.fromisoformat(str(cached.data["asOf"])),
            cached.provider,
            cached.is_stale or force_stale,
        )

    async def get_total_return_series(
        self,
        tickers: Iterable[str],
        *,
        lookback_years: int,
        refresh: bool = False,
    ) -> dict[str, WebTotalReturnSeries]:
        pairs = await asyncio.gather(
            *(
                self._get_total_return_series(
                    ticker, lookback_years=lookback_years, refresh=refresh
                )
                for ticker in sorted(set(tickers))
            )
        )
        return {item.ticker: item for item in pairs if item is not None}

    async def _get_total_return_series(
        self, ticker: str, *, lookback_years: int, refresh: bool
    ) -> WebTotalReturnSeries | None:
        provider_name = self.provider.name if self.provider else "yfinance"
        cache_key = f"US:{ticker}:{lookback_years}Y"
        cached = await self.repository.get_market_cache(
            provider=provider_name,
            namespace="WEB_TOTAL_RETURN",
            cache_key=cache_key,
            allow_stale=refresh or self.provider is None,
        )
        if cached is not None and not refresh and not cached.is_stale:
            return self._cached_series(ticker, cached)
        if self.provider is None:
            return self._cached_series(ticker, cached)
        try:
            async with self._semaphore:
                series = await self.provider.get_total_return_series(
                    "US", ticker, lookback_years=lookback_years
                )
            now = datetime.now(timezone.utc)
            data = {
                "currency": series.currency,
                "asOf": series.as_of.isoformat(),
                "seriesKind": series.series_kind,
                "points": [
                    [point.trading_date.isoformat(), str(point.value)] for point in series.points
                ],
            }
            await self.repository.put_market_cache(
                provider=provider_name,
                namespace="WEB_TOTAL_RETURN",
                cache_key=cache_key,
                normalized_data=data,
                fetched_at=now,
                expires_at=now + timedelta(hours=24),
                stale_until=now + timedelta(days=7),
            )
            return WebTotalReturnSeries(
                ticker,
                series.currency,
                series.as_of,
                series.provider,
                series.series_kind,
                tuple((point.trading_date, point.value) for point in series.points),
                series.stale,
            )
        except MarketDataError:
            fallback = await self.repository.get_market_cache(
                provider=provider_name,
                namespace="WEB_TOTAL_RETURN",
                cache_key=cache_key,
                allow_stale=True,
            )
            return self._cached_series(ticker, fallback, force_stale=True)

    @staticmethod
    def _cached_series(
        ticker: str, cached, *, force_stale: bool = False
    ) -> WebTotalReturnSeries | None:
        if cached is None:
            return None
        return WebTotalReturnSeries(
            ticker=ticker,
            currency=str(cached.data["currency"]),
            as_of=datetime.fromisoformat(str(cached.data["asOf"])),
            provider=cached.provider,
            series_kind=str(cached.data["seriesKind"]),
            points=tuple(
                (date.fromisoformat(str(day)), Decimal(str(value)))
                for day, value in cached.data["points"]
            ),
            stale=cached.is_stale or force_stale,
        )

    async def get_distribution_series(
        self,
        tickers: Iterable[str],
        *,
        lookback_years: int = 5,
        refresh: bool = False,
    ) -> dict[str, WebDistributionSeries]:
        pairs = await asyncio.gather(
            *(
                self._get_distribution_series(
                    ticker, lookback_years=lookback_years, refresh=refresh
                )
                for ticker in sorted(set(tickers))
            )
        )
        return {item.ticker: item for item in pairs if item is not None}

    async def _get_distribution_series(
        self, ticker: str, *, lookback_years: int, refresh: bool
    ) -> WebDistributionSeries | None:
        provider_name = self.provider.name if self.provider else "yfinance"
        cache_key = f"US:{ticker}:{lookback_years}Y"
        cached = await self.repository.get_market_cache(
            provider=provider_name,
            namespace="WEB_DISTRIBUTION",
            cache_key=cache_key,
            allow_stale=refresh or self.provider is None,
        )
        if cached is not None and not refresh and not cached.is_stale:
            return self._cached_distribution(ticker, cached)
        if self.provider is None:
            return self._cached_distribution(ticker, cached)
        try:
            async with self._semaphore:
                series = await self.provider.get_distribution_series(
                    "US", ticker, lookback_years=lookback_years
                )
            if series.currency != "USD":
                return None
            now = datetime.now(timezone.utc)
            data = {
                "currency": series.currency,
                "asOf": series.as_of.isoformat(),
                "coverageStart": series.coverage_start.isoformat(),
                "coverageEnd": series.coverage_end.isoformat(),
                "points": [
                    [
                        point.ex_date.isoformat(),
                        str(point.amount_per_share),
                        point.classification,
                    ]
                    for point in series.points
                ],
            }
            await self.repository.put_market_cache(
                provider=provider_name,
                namespace="WEB_DISTRIBUTION",
                cache_key=cache_key,
                normalized_data=data,
                fetched_at=now,
                expires_at=now + timedelta(hours=24),
                stale_until=now + timedelta(days=7),
            )
            return WebDistributionSeries(
                ticker=ticker,
                currency=series.currency,
                as_of=series.as_of,
                provider=series.provider,
                coverage_start=series.coverage_start,
                coverage_end=series.coverage_end,
                points=tuple(
                    (point.ex_date, point.amount_per_share, point.classification)
                    for point in series.points
                ),
                stale=series.stale,
            )
        except MarketDataError:
            fallback = await self.repository.get_market_cache(
                provider=provider_name,
                namespace="WEB_DISTRIBUTION",
                cache_key=cache_key,
                allow_stale=True,
            )
            return self._cached_distribution(ticker, fallback, force_stale=True)

    @staticmethod
    def _cached_distribution(
        ticker: str, cached, *, force_stale: bool = False
    ) -> WebDistributionSeries | None:
        if cached is None:
            return None
        return WebDistributionSeries(
            ticker=ticker,
            currency=str(cached.data["currency"]),
            as_of=datetime.fromisoformat(str(cached.data["asOf"])),
            provider=cached.provider,
            coverage_start=date.fromisoformat(str(cached.data["coverageStart"])),
            coverage_end=date.fromisoformat(str(cached.data["coverageEnd"])),
            points=tuple(
                (date.fromisoformat(str(day)), Decimal(str(amount)), str(kind))
                for day, amount, kind in cached.data["points"]
            ),
            stale=cached.is_stale or force_stale,
        )
