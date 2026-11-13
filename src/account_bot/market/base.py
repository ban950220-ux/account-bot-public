"""Provider-neutral market-data contracts.

Broker adapters are the source of truth for holdings, cost basis and executions.
Implementations of this module may only enrich those holdings with public market
data; they must never be used to infer that a broker position exists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MarketDataError(RuntimeError):
    """Sanitized provider failure safe to surface without response bodies."""


@dataclass(frozen=True, slots=True)
class PriceQuote:
    market: str
    ticker: str
    currency: str
    price: Decimal
    as_of: datetime
    provider: str
    stale: bool = False


@dataclass(frozen=True, slots=True)
class TrendData:
    """Adjusted-price statistics used only for long-horizon DCA comparison."""

    market: str
    ticker: str
    as_of: datetime
    provider: str
    observation_count: int
    return_1y: Decimal | None = None
    cagr_3y: Decimal | None = None
    max_drawdown_3y: Decimal | None = None
    annualized_volatility_1y: Decimal | None = None
    above_200d_average: bool | None = None


@dataclass(frozen=True, slots=True)
class TotalReturnPoint:
    trading_date: date
    value: Decimal


@dataclass(frozen=True, slots=True)
class TotalReturnSeries:
    """Dividend/split-adjusted index input with explicit provenance."""

    market: str
    ticker: str
    currency: str
    as_of: datetime
    provider: str
    series_kind: str
    coverage_start: date
    coverage_end: date
    points: tuple[TotalReturnPoint, ...]
    stale: bool = False


@dataclass(frozen=True, slots=True)
class DistributionPoint:
    ex_date: date
    amount_per_share: Decimal
    classification: str = "UNCLASSIFIED_DISTRIBUTION"


@dataclass(frozen=True, slots=True)
class DistributionSeries:
    """Per-share cash distributions with explicit coverage and provenance."""

    market: str
    ticker: str
    currency: str
    as_of: datetime
    provider: str
    coverage_start: date
    coverage_end: date
    points: tuple[DistributionPoint, ...]
    stale: bool = False


@dataclass(frozen=True, slots=True)
class FxQuote:
    """FX rate in quote units for one unit of base (USD/KRW -> KRW per USD)."""

    base: str
    quote: str
    rate: Decimal
    as_of: datetime
    provider: str
    stale: bool = False

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("FX rate must be positive")


@dataclass(frozen=True, slots=True)
class FundamentalData:
    market: str
    ticker: str
    as_of: datetime
    provider: str
    revenue_growth: Decimal | None = None
    earnings_growth: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    free_cash_flow: Decimal | None = None
    return_on_equity: Decimal | None = None
    return_on_assets: Decimal | None = None
    total_cash: Decimal | None = None
    total_debt: Decimal | None = None
    debt_to_equity: Decimal | None = None
    market_cap: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    raw_coverage: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class ValuationData:
    market: str
    ticker: str
    as_of: datetime
    provider: str
    trailing_pe: Decimal | None = None
    forward_pe: Decimal | None = None
    peg: Decimal | None = None
    price_to_sales: Decimal | None = None
    enterprise_to_ebitda: Decimal | None = None
    enterprise_to_sales: Decimal | None = None
    free_cash_flow_yield: Decimal | None = None
    historical_forward_pe_median: Decimal | None = None
    historical_forward_pe_percentile: Decimal | None = None
    peer_forward_pe_median: Decimal | None = None
    close_price: Decimal | None = None
    dilution_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EarningsData:
    market: str
    ticker: str
    period: str | None
    announced_at: datetime | None
    as_of: datetime
    provider: str
    revenue: Decimal | None = None
    revenue_yoy: Decimal | None = None
    eps: Decimal | None = None
    eps_yoy: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    free_cash_flow: Decimal | None = None
    guidance: str | None = None
    guidance_change: str | None = None
    management_commentary: str | None = None


@dataclass(frozen=True, slots=True)
class NewsItem:
    ticker: str
    title: str
    url: str
    published_at: datetime | None
    publisher: str | None
    summary: str | None = None
    fundamental_impact: str = "UNASSESSED"
    impact_reasons: tuple[str, ...] = field(default_factory=tuple)


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        raise NotImplementedError

    @abstractmethod
    async def get_fx(self, base: str, quote: str) -> FxQuote:
        raise NotImplementedError

    @abstractmethod
    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:
        raise NotImplementedError

    @abstractmethod
    async def get_valuation(self, market: str, ticker: str) -> ValuationData:
        raise NotImplementedError

    @abstractmethod
    async def get_earnings(self, market: str, ticker: str) -> EarningsData:
        raise NotImplementedError

    @abstractmethod
    async def get_news(self, market: str, ticker: str, *, limit: int = 10) -> list[NewsItem]:
        raise NotImplementedError

    async def get_trend(self, market: str, ticker: str) -> TrendData:
        del market, ticker
        raise MarketDataError("trend data is not supported by this provider")

    async def get_total_return_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 10,
    ) -> TotalReturnSeries:
        del market, ticker, lookback_years
        raise MarketDataError("total-return data is not supported by this provider")

    async def get_distribution_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 5,
    ) -> DistributionSeries:
        del market, ticker, lookback_years
        raise MarketDataError("distribution data is not supported by this provider")


def json_ready(value: Any) -> Any:
    """Convert provider dataclasses into lossless JSON-compatible values."""

    if hasattr(value, "__dataclass_fields__"):
        return json_ready(asdict(value))
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    return value


def restore_json(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__decimal__"}:
        return Decimal(value["__decimal__"])
    if isinstance(value, dict) and set(value) == {"__datetime__"}:
        return datetime.fromisoformat(value["__datetime__"])
    if isinstance(value, dict):
        return {key: restore_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_json(item) for item in value]
    return value
