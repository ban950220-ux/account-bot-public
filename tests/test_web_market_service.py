from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from account_bot.market import (
    DistributionPoint,
    DistributionSeries,
    EarningsData,
    FundamentalData,
    FxQuote,
    MarketDataError,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    TotalReturnPoint,
    TotalReturnSeries,
    ValuationData,
)
from account_bot.storage import Database, SQLitePortfolioRepository
from account_bot.web_api.market_service import WebMarketDataService


class _SwitchableProvider(MarketDataProvider):
    name = "switchable"

    def __init__(self) -> None:
        self.failing = False

    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        if self.failing:
            raise MarketDataError("failed")
        return PriceQuote(
            market,
            ticker,
            "USD",
            Decimal("25"),
            datetime(2026, 9, 12, tzinfo=timezone.utc),
            self.name,
        )

    async def get_fx(self, base: str, quote: str) -> FxQuote:
        if self.failing:
            raise MarketDataError("failed")
        return FxQuote(
            base,
            quote,
            Decimal("1400"),
            datetime(2026, 9, 12, tzinfo=timezone.utc),
            self.name,
        )

    async def get_total_return_series(
        self, market: str, ticker: str, *, lookback_years: int = 10
    ) -> TotalReturnSeries:
        if self.failing:
            raise MarketDataError("failed")
        points = (
            TotalReturnPoint(date(2025, 1, 2), Decimal("100")),
            TotalReturnPoint(date(2026, 1, 2), Decimal("110")),
        )
        return TotalReturnSeries(
            market,
            ticker,
            "USD",
            datetime(2026, 9, 12, tzinfo=timezone.utc),
            self.name,
            "ADJUSTED_TOTAL_RETURN",
            points[0].trading_date,
            points[-1].trading_date,
            points,
        )

    async def get_distribution_series(
        self, market: str, ticker: str, *, lookback_years: int = 5
    ) -> DistributionSeries:
        if self.failing:
            raise MarketDataError("failed")
        return DistributionSeries(
            market,
            ticker,
            "USD",
            datetime(2026, 9, 12, tzinfo=timezone.utc),
            self.name,
            date(2025, 1, 2),
            date(2026, 9, 12),
            (DistributionPoint(date(2026, 8, 1), Decimal("0.50")),),
        )

    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:
        raise NotImplementedError

    async def get_valuation(self, market: str, ticker: str) -> ValuationData:
        raise NotImplementedError

    async def get_earnings(self, market: str, ticker: str) -> EarningsData:
        raise NotImplementedError

    async def get_news(
        self, market: str, ticker: str, *, limit: int = 10
    ) -> list[NewsItem]:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_refresh_failure_uses_persisted_fx_and_price_as_stale(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cache.db').as_posix()}")
    await database.initialize()
    repository = SQLitePortfolioRepository(database)
    await repository.initialize_default_portfolio()
    provider = _SwitchableProvider()
    service = WebMarketDataService(repository, provider)
    try:
        first_fx = await service.get_fx(refresh=True)
        first_price = (await service.get_prices(["AAA"], refresh=True))["AAA"]
        first_series = (await service.get_total_return_series(
            ["AAA"], lookback_years=10, refresh=True
        ))["AAA"]
        first_distribution = (await service.get_distribution_series(
            ["AAA"], refresh=True
        ))["AAA"]
        assert first_fx is not None and not first_fx.stale
        assert not first_price.stale
        assert not first_series.stale
        assert not first_distribution.stale

        provider.failing = True
        fallback_fx = await service.get_fx(refresh=True)
        fallback_price = (await service.get_prices(["AAA"], refresh=True))["AAA"]
        fallback_series = (await service.get_total_return_series(
            ["AAA"], lookback_years=10, refresh=True
        ))["AAA"]
        fallback_distribution = (await service.get_distribution_series(
            ["AAA"], refresh=True
        ))["AAA"]
        assert fallback_fx is not None and fallback_fx.stale
        assert fallback_price.stale
        assert fallback_series.stale
        assert fallback_distribution.stale
        assert fallback_fx.rate == Decimal("1400")
        assert fallback_price.price == Decimal("25")
    finally:
        await database.dispose()
