"""Read models and mutations for the local portfolio web API."""

from __future__ import annotations

import asyncio
import functools
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from account_bot.projection import (
    BootstrapPositionInput,
    DeterministicProjectionEngine,
    HistoricalBlockBootstrapEngine,
    ProjectionInput,
    TradingCalendarProvider,
)
from account_bot.storage import (
    Database,
    NormalizedPortfolioImportPosition,
    PortfolioImportBatch,
    SQLitePortfolioRepository,
)

from .market_service import WebMarketDataService

_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,31}$")
_WEB_PLAN_PREFIX = "web-dca:US:"

_CATEGORIES = {
    "VOO": "Core Index",
    "QQQ": "Growth",
    "GRNY": "Growth",
    "SPMO": "Growth",
    "VUG": "Growth",
    "IWM": "Small Cap",
    "VTWO": "Small Cap",
    "SCHD": "Dividend",
    "DGRO": "Dividend",
    "VIG": "Dividend",
    "VYM": "Dividend",
    "JEPQ": "Covered Call / Income",
    "JEPI": "Covered Call / Income",
    "QYLD": "Covered Call / Income",
    "QDTE": "Covered Call / Income",
    "FEPI": "Covered Call / Income",
    "QQQI": "Covered Call / Income",
    "GPIQ": "Covered Call / Income",
    "QDVO": "Covered Call / Income",
    "ISPY": "Covered Call / Income",
    "IQQQ": "Covered Call / Income",
    "DIVO": "Covered Call / Income",
    "XLF": "Sector",
    "XLV": "Sector",
    "VGT": "Sector",
    "SOXX": "Sector",
    "SOXQ": "Sector",
    "PAVE": "Sector",
    "IEF": "Bond",
    "TLT": "Bond",
    "GLDM": "Commodity / Alternative",
}


def decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def parse_decimal_string(value: str, *, field: str) -> Decimal:
    try:
        result = Decimal(value.strip())
    except (AttributeError, InvalidOperation):
        raise ValueError(f"{field} must be a decimal string") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


@dataclass(slots=True)
class _PositionAccumulator:
    market: str
    ticker: str
    asset_type: str
    currency: str
    quantity: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    contribution: Decimal = Decimal("0")
    source_count: int = 0
    warning_codes: set[str] | None = None

    def __post_init__(self) -> None:
        if self.warning_codes is None:
            self.warning_codes = set()


class PortfolioWebService:
    def __init__(
        self,
        database: Database,
        repository: SQLitePortfolioRepository,
        market_data: WebMarketDataService | None = None,
        trading_calendar: TradingCalendarProvider | None = None,
    ) -> None:
        self.database = database
        self.repository = repository
        self.market_data = market_data
        self.trading_calendar = trading_calendar
        self._monte_carlo_cache: dict[tuple[object, ...], dict[str, Any]] = {}

    async def get_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        async with self.database.session() as session:
            batch = await session.scalar(
                select(PortfolioImportBatch)
                .where(PortfolioImportBatch.status == "READY")
                .order_by(PortfolioImportBatch.imported_at.desc(), PortfolioImportBatch.id.desc())
                .limit(1)
            )
            rows = []
            if batch is not None:
                rows = list(
                    (
                        await session.scalars(
                            select(NormalizedPortfolioImportPosition)
                            .where(NormalizedPortfolioImportPosition.import_id == batch.id)
                            .order_by(NormalizedPortfolioImportPosition.ticker)
                        )
                    ).all()
                )

        groups: dict[tuple[str, str, str, str], _PositionAccumulator] = {}
        for row in rows:
            key = (row.market, row.ticker, row.asset_type, row.currency)
            group = groups.setdefault(key, _PositionAccumulator(*key))
            group.quantity += row.quantity
            group.cost += row.total_cost_basis_usd
            group.market_value += row.import_market_value_usd
            group.contribution += row.daily_contribution_krw
            group.source_count += 1
            assert group.warning_codes is not None
            group.warning_codes.update(row.warning_codes)

        plan_views = await self.repository.list_recurring_buy_views(enabled_only=False)
        overrides = {
            view.ticker: view
            for view in plan_views
            if view.plan.plan_key.startswith(_WEB_PLAN_PREFIX)
        }
        for ticker, view in overrides.items():
            key = (view.market, ticker, view.asset_type, view.instrument_currency)
            groups.setdefault(key, _PositionAccumulator(*key))

        prices = (
            await self.market_data.get_prices(
                (group.ticker for group in groups.values() if group.quantity > 0),
                refresh=refresh,
            )
            if self.market_data is not None
            else {}
        )
        fx = (
            await self.market_data.get_fx(refresh=refresh)
            if self.market_data is not None
            else None
        )

        positions = []
        for group in groups.values():
            override = overrides.get(group.ticker)
            configured = override.plan.daily_amount if override else group.contribution
            enabled = override.plan.enabled if override else group.contribution > 0
            effective = configured if enabled else Decimal("0")
            quote = prices.get(group.ticker)
            current_price = (
                quote.price
                if quote is not None
                else (group.market_value / group.quantity if group.quantity > 0 else None)
            )
            current_value = (
                group.quantity * current_price
                if current_price is not None
                else group.market_value
            )
            import_profit = group.market_value - group.cost
            current_profit = current_value - group.cost
            positions.append(
                {
                    "instrument": {
                        "market": group.market,
                        "ticker": group.ticker,
                        "assetType": group.asset_type,
                        "currency": group.currency,
                    },
                    "category": _CATEGORIES.get(group.ticker, "기타"),
                    "quantity": decimal_string(group.quantity),
                    "totalCostBasisUSD": decimal_string(group.cost),
                    "importMarketValueUSD": decimal_string(group.market_value),
                    "averageCostPerShareUSD": decimal_string(
                        group.cost / group.quantity if group.quantity > 0 else None
                    ),
                    "importCurrentPriceUSD": decimal_string(
                        group.market_value / group.quantity if group.quantity > 0 else None
                    ),
                    "latestCurrentPriceUSD": decimal_string(current_price),
                    "currentMarketValueUSD": decimal_string(current_value),
                    "currentMarketValueKRW": decimal_string(
                        current_value * fx.rate if fx is not None else None
                    ),
                    "priceStatus": (
                        "STALE" if quote is not None and quote.stale else
                        "LIVE" if quote is not None else
                        "IMPORT_SNAPSHOT" if group.quantity > 0 else "UNAVAILABLE"
                    ),
                    "priceProvider": quote.provider if quote is not None else None,
                    "priceAsOf": quote.as_of.isoformat() if quote is not None else None,
                    "importProfitUSD": decimal_string(import_profit),
                    "calculatedImportReturnPercent": decimal_string(
                        (group.market_value / group.cost - Decimal("1")) * Decimal("100")
                        if group.cost > 0
                        else None
                    ),
                    "currentProfitUSD": decimal_string(current_profit),
                    "currentReturnPercent": decimal_string(
                        (current_value / group.cost - Decimal("1")) * Decimal("100")
                        if group.cost > 0
                        else None
                    ),
                    "configuredContributionKRW": decimal_string(configured),
                    "effectiveContributionKRW": decimal_string(effective),
                    "dailyBuyEnabled": enabled,
                    "sourceCount": group.source_count,
                    "warningCodes": sorted(group.warning_codes or ()),
                }
            )

        positions.sort(key=lambda item: Decimal(item["currentMarketValueUSD"]), reverse=True)

        total_cost = sum((Decimal(item["totalCostBasisUSD"]) for item in positions), Decimal())
        total_value = sum(
            (Decimal(item["currentMarketValueUSD"]) for item in positions), Decimal()
        )
        daily = sum(
            (Decimal(item["effectiveContributionKRW"]) for item in positions), Decimal()
        )
        return {
            "dataState": "READY" if groups else "EMPTY",
            "asOf": max(
                (quote.as_of for quote in prices.values()),
                default=batch.imported_at if batch is not None else None,
            ).isoformat() if prices or batch is not None else None,
            "sourceImportId": batch.import_key if batch is not None else None,
            "priceSource": (
                "LIVE" if sum(group.quantity > 0 for group in groups.values()) > 0
                and len(prices) == sum(group.quantity > 0 for group in groups.values())
                else "MIXED" if prices
                else "IMPORT_SNAPSHOT" if batch is not None
                else "UNAVAILABLE"
            ),
            "fx": {
                "status": (
                    "UNAVAILABLE" if fx is None else "STALE" if fx.stale else "LIVE"
                ),
                "rate": decimal_string(fx.rate) if fx is not None else None,
                "provider": fx.provider if fx is not None else None,
                "asOf": fx.as_of.isoformat() if fx is not None else None,
                "message": (
                    "환율 데이터를 불러오지 못했습니다. USD 결과는 계속 사용할 수 있습니다."
                    if fx is None
                    else "마지막 정상 환율을 사용 중입니다."
                    if fx.stale
                    else "최신 USD/KRW 환율을 적용했습니다."
                ),
            },
            "summary": {
                "totalCostBasisUSD": decimal_string(total_cost),
                "totalMarketValueUSD": decimal_string(total_value),
                "totalMarketValueKRW": decimal_string(
                    total_value * fx.rate if fx is not None else None
                ),
                "totalCostBasisKRW": decimal_string(
                    total_cost * fx.rate if fx is not None else None
                ),
                "totalProfitUSD": decimal_string(total_value - total_cost),
                "totalReturnPercent": decimal_string(
                    (total_value / total_cost - Decimal("1")) * Decimal("100")
                    if total_cost > 0
                    else None
                ),
                "dailyContributionKRW": decimal_string(daily),
                "monthlyAverageContributionKRW": decimal_string(
                    daily * Decimal("252") / Decimal("12")
                ),
                "annualContributionKRW": decimal_string(daily * Decimal("252")),
                "holdingCount": sum(Decimal(item["quantity"]) > 0 for item in positions),
                "investmentTargetCount": len(positions),
            },
            "positions": positions,
            "disclaimer": (
                "시장가격을 우선 적용하며 조회 실패 종목은 Excel snapshot 또는 마지막 정상값을 사용합니다."
                if prices
                else "Excel 가격은 가져온 시점의 snapshot이며 실시간 현재가가 아닙니다."
                if batch is not None
                else "보유자산 snapshot이 없어 매일모으기 계획만 표시합니다."
            ),
        }

    async def update_daily_buy(
        self,
        ticker: str,
        *,
        configured_amount_krw: str,
        enabled: bool,
    ) -> dict[str, Any]:
        normalized_ticker = ticker.strip().upper()
        if not _TICKER.fullmatch(normalized_ticker):
            raise ValueError("ticker is malformed")
        amount = parse_decimal_string(configured_amount_krw, field="configuredAmountKRW")
        plan_key = f"{_WEB_PLAN_PREFIX}{normalized_ticker}:ETF:USD"
        if amount < 0:
            raise ValueError("configuredAmountKRW must not be negative")
        if amount == 0:
            existing = await self.repository.get_recurring_buy(plan_key)
            if existing is None:
                raise ValueError("a new plan requires a positive configured amount")
            amount = existing.daily_amount
            enabled = False
        plan = await self.repository.upsert_recurring_buy(
            plan_key=plan_key,
            market="US",
            ticker=normalized_ticker,
            asset_type="ETF",
            instrument_currency="USD",
            daily_amount=amount,
            currency="KRW",
            enabled=enabled,
            source="WEB_DCA",
            memo="Managed by the local portfolio dashboard",
        )
        return {
            "ticker": normalized_ticker,
            "configuredAmountKRW": decimal_string(plan.daily_amount),
            "effectiveContributionKRW": decimal_string(
                plan.daily_amount if plan.enabled else Decimal("0")
            ),
            "enabled": plan.enabled,
            "version": plan.version,
        }

    async def get_performance(
        self,
        *,
        lookback_years: int = 10,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if lookback_years not in {1, 3, 5, 10}:
            raise ValueError("lookbackYears must be one of 1, 3, 5, or 10")
        snapshot = await self.get_snapshot(refresh=False)
        holdings = [
            item
            for item in snapshot["positions"]
            if Decimal(item["quantity"]) > 0 and Decimal(item["currentMarketValueUSD"]) > 0
        ]
        if self.market_data is None or not holdings:
            return self._unavailable_performance(lookback_years, "Total Return 데이터 없음")
        series_by_ticker = await self.market_data.get_total_return_series(
            (item["instrument"]["ticker"] for item in holdings),
            lookback_years=10,
            refresh=refresh,
        )
        if not series_by_ticker:
            return self._unavailable_performance(
                lookback_years, "Total Return provider에서 시계열을 받지 못했습니다."
            )
        dated_usd_series = [
            series
            for series in series_by_ticker.values()
            if series.currency == "USD" and series.points
        ]
        if not dated_usd_series:
            return self._unavailable_performance(
                lookback_years,
                "USD Total Return 시계열이 없습니다.",
            )
        evaluation_day = min(
            series.points[-1][0] for series in dated_usd_series
        )
        try:
            cutoff = evaluation_day.replace(year=evaluation_day.year - lookback_years)
        except ValueError:
            cutoff = evaluation_day.replace(
                year=evaluation_day.year - lookback_years,
                day=28,
            )
        grace = cutoff + timedelta(days=45)
        values = {
            item["instrument"]["ticker"]: Decimal(item["currentMarketValueUSD"])
            for item in holdings
        }
        total_value = sum(values.values(), Decimal("0"))
        eligible = {
            ticker: series
            for ticker, series in series_by_ticker.items()
            if series.currency == "USD" and series.points[0][0] <= grace
        }
        covered_value = sum((values.get(ticker, Decimal("0")) for ticker in eligible), Decimal())
        if not eligible or covered_value <= 0:
            return self._unavailable_performance(
                lookback_years,
                "선택 기간 전체를 충족하는 ETF 시계열이 없습니다.",
            )
        start = max(max(cutoff, series.points[0][0]) for series in eligible.values())
        tracks: dict[str, list[tuple[date, Decimal]]] = {
            ticker: [(day, value) for day, value in series.points if day >= start]
            for ticker, series in eligible.items()
        }
        tracks = {ticker: points for ticker, points in tracks.items() if points}
        if not tracks:
            return self._unavailable_performance(lookback_years, "공통 거래일 데이터가 없습니다.")
        weights = {ticker: values[ticker] for ticker in tracks}
        weight_total = sum(weights.values(), Decimal())
        bases = {ticker: points[0][1] for ticker, points in tracks.items()}
        by_date = {
            ticker: {day: value for day, value in points} for ticker, points in tracks.items()
        }
        dates = sorted({day for points in tracks.values() for day, _ in points})
        last: dict[str, Decimal] = {}
        index_values: list[tuple[date, Decimal]] = []
        for day in dates:
            for ticker, mapping in by_date.items():
                if day in mapping:
                    last[ticker] = mapping[day]
            if len(last) != len(tracks):
                continue
            level = sum(
                (weights[ticker] * last[ticker] / bases[ticker] for ticker in tracks),
                Decimal(),
            ) / weight_total
            index_values.append((day, level))
        if len(index_values) < 2:
            return self._unavailable_performance(lookback_years, "수익률 계산 구간이 부족합니다.")
        first_level = index_values[0][1]
        normalized = [(day, level / first_level) for day, level in index_values]
        total_return = (normalized[-1][1] - Decimal("1")) * Decimal("100")
        elapsed_years = Decimal((normalized[-1][0] - normalized[0][0]).days) / Decimal(
            "365.2425"
        )
        cagr = (
            ((normalized[-1][1].ln() / elapsed_years).exp() - Decimal("1"))
            * Decimal("100")
            if elapsed_years > 0
            else None
        )
        peak = normalized[0][1]
        max_drawdown = Decimal("0")
        for _, level in normalized:
            peak = max(peak, level)
            max_drawdown = min(max_drawdown, level / peak - Decimal("1"))
        stride = max(1, len(normalized) // 260)
        sampled = normalized[::stride]
        if sampled[-1] != normalized[-1]:
            sampled.append(normalized[-1])
        coverage = covered_value / total_value * Decimal("100") if total_value else Decimal("0")
        warnings = []
        if any(item.stale for item in eligible.values()):
            warnings.append("STALE_TOTAL_RETURN_DATA")
        if coverage < Decimal("80"):
            warnings.append("LOW_PORTFOLIO_COVERAGE")
        if elapsed_years < Decimal("3"):
            warnings.append("SHORT_HISTORY_LONG_TERM_EXTRAPOLATION")
        return {
            "status": "READY",
            "dataStatus": "STALE" if "STALE_TOTAL_RETURN_DATA" in warnings else "LIVE",
            "lookbackYears": lookback_years,
            "seriesKind": "ADJUSTED_TOTAL_RETURN",
            "provider": sorted({item.provider for item in eligible.values()}),
            "asOf": max(item.as_of for item in eligible.values()).isoformat(),
            "coverageStart": normalized[0][0].isoformat(),
            "coverageEnd": normalized[-1][0].isoformat(),
            "dataCoveragePercent": decimal_string(coverage),
            "includedTickers": sorted(tracks),
            "excludedTickers": sorted(set(values) - set(tracks)),
            "totalReturnPercent": decimal_string(total_return),
            "cagrPercent": decimal_string(cagr),
            "maxDrawdownPercent": decimal_string(max_drawdown * Decimal("100")),
            "points": [
                {
                    "date": day.isoformat(),
                    "index": decimal_string(level * Decimal("100")),
                    "cumulativeReturnPercent": decimal_string(
                        (level - Decimal("1")) * Decimal("100")
                    ),
                }
                for day, level in sampled
            ],
            "warnings": warnings,
            "disclaimer": (
                "배당·분할 조정 시계열 기반 과거 Total Return입니다. "
                "현재 보유비중을 고정해 재구성했으며 미래수익을 의미하지 않습니다."
            ),
        }

    @staticmethod
    def _unavailable_performance(lookback_years: int, message: str) -> dict[str, Any]:
        return {
            "status": "UNAVAILABLE",
            "lookbackYears": lookback_years,
            "seriesKind": None,
            "points": [],
            "warnings": ["TOTAL_RETURN_UNAVAILABLE"],
            "message": message,
        }

    async def get_projection(
        self,
        *,
        refresh: bool = False,
        contribution_overrides: dict[str, tuple[Decimal, bool]] | None = None,
    ) -> dict[str, Any]:
        if self.market_data is None or self.trading_calendar is None:
            return {
                "status": "UNAVAILABLE",
                "message": "시장데이터 또는 미국 거래일 calendar가 연결되지 않았습니다.",
            }
        snapshot = await self.get_snapshot(refresh=False)
        fx_rate = snapshot["fx"]["rate"]
        if fx_rate is None:
            return {
                "status": "UNAVAILABLE",
                "message": "KRW 매일모으기를 변환할 USD/KRW 환율이 없습니다.",
            }
        overrides = contribution_overrides or {}
        holdings = [
            item
            for item in snapshot["positions"]
            if Decimal(item["currentMarketValueUSD"]) > 0
            or Decimal(item["effectiveContributionKRW"]) > 0
            or item["instrument"]["ticker"] in overrides
        ]
        tickers = tuple(item["instrument"]["ticker"] for item in holdings)
        series_by_ticker, distributions_by_ticker = await asyncio.gather(
            self.market_data.get_total_return_series(
                tickers,
                lookback_years=10,
                refresh=refresh,
            ),
            self.market_data.get_distribution_series(
                tickers,
                lookback_years=5,
                refresh=refresh,
            ),
        )
        inputs: list[ProjectionInput] = []
        excluded: list[str] = []
        for item in holdings:
            ticker = item["instrument"]["ticker"]
            series = series_by_ticker.get(ticker)
            if series is None or len(series.points) < 2:
                excluded.append(ticker)
                continue
            first_day, first_value = series.points[0]
            last_day, last_value = series.points[-1]
            history_years = Decimal((last_day - first_day).days) / Decimal("365.2425")
            if history_years <= 0 or first_value <= 0 or last_value <= 0:
                excluded.append(ticker)
                continue
            annual_return = (last_value / first_value).ln() / history_years
            annual_return = annual_return.exp() - Decimal("1")
            inputs.append(
                ProjectionInput(
                    ticker=ticker,
                    current_value_usd=Decimal(item["currentMarketValueUSD"]),
                    daily_contribution_krw=(
                        overrides[ticker][0] if overrides[ticker][1] else Decimal("0")
                    )
                    if ticker in overrides
                    else Decimal(item["effectiveContributionKRW"]),
                    annual_return=annual_return,
                    history_years=history_years,
                )
            )
        if not inputs:
            return {
                "status": "UNAVAILABLE",
                "message": "Projection에 사용할 Total Return 시계열이 없습니다.",
            }
        engine = DeterministicProjectionEngine(self.trading_calendar)
        as_of = date.today()
        trailing_cutoff = as_of - timedelta(days=365)
        distribution_yields: dict[str, Decimal] = {}
        for item in holdings:
            ticker = item["instrument"]["ticker"]
            series = distributions_by_ticker.get(ticker)
            current_price = item["latestCurrentPriceUSD"]
            if (
                series is None
                or series.currency != "USD"
                or series.coverage_start > trailing_cutoff + timedelta(days=45)
                or current_price is None
                or Decimal(current_price) <= 0
            ):
                continue
            trailing_per_share = sum(
                (
                    amount
                    for ex_date, amount, _ in series.points
                    if trailing_cutoff < ex_date <= as_of
                ),
                Decimal(),
            )
            distribution_yields[ticker] = trailing_per_share / Decimal(current_price)
        horizons = {}
        income_horizons = {}
        for years in (1, 3, 5, 7, 10):
            try:
                end = as_of.replace(year=as_of.year + years)
            except ValueError:
                end = as_of.replace(year=as_of.year + years, day=28)
            result = engine.project(
                tuple(inputs),
                as_of=as_of,
                horizon_date=end,
                usd_krw=Decimal(fx_rate),
            )
            horizon_key = f"{years}Y"
            horizons[horizon_key] = self._serialize_projection_result(result, fx_rate)
            annual_income_usd = sum(
                (
                    row["endingValueUSD"] * distribution_yields.get(row["ticker"], Decimal())
                    for row in result["positions"]
                ),
                Decimal(),
            )
            income_horizons[horizon_key] = self._serialize_income(
                annual_income_usd, Decimal(fx_rate)
            )
        ten_year_end = as_of.replace(year=as_of.year + 10)
        daily_timeline = engine.project_timeline(
            tuple(inputs),
            as_of=as_of,
            horizon_date=ten_year_end,
            usd_krw=Decimal(fx_rate),
            sample_monthly=False,
        )
        chart_timeline = []
        for index, point in enumerate(daily_timeline):
            is_last = index == len(daily_timeline) - 1
            is_month_end = (
                is_last
                or daily_timeline[index + 1]["date"].month != point["date"].month
            )
            if index == 0 or is_month_end:
                chart_timeline.append(self._serialize_timeline_point(point, fx_rate))
        milestones = []
        for target_krw in (
            Decimal("100000000"),
            Decimal("200000000"),
            Decimal("300000000"),
            Decimal("500000000"),
            Decimal("1000000000"),
        ):
            reached = next(
                (
                    point
                    for point in daily_timeline
                    if point["endingValueUSD"] * Decimal(fx_rate) >= target_krw
                ),
                None,
            )
            milestones.append(
                {
                    "targetKRW": decimal_string(target_krw),
                    "reachedDate": reached["date"].isoformat() if reached else None,
                    "status": "REACHED" if reached else "NOT_REACHED_WITHIN_10Y",
                }
            )
        covered_value = sum((item.current_value_usd for item in inputs), Decimal())
        total_value = Decimal(snapshot["summary"]["totalMarketValueUSD"])
        distribution_covered_value = sum(
            (
                Decimal(item["currentMarketValueUSD"])
                for item in holdings
                if item["instrument"]["ticker"] in distribution_yields
            ),
            Decimal(),
        )
        current_annual_income_usd = sum(
            (
                Decimal(item["currentMarketValueUSD"])
                * distribution_yields.get(item["instrument"]["ticker"], Decimal())
                for item in holdings
            ),
            Decimal(),
        )
        income_stale = any(
            series.stale
            for ticker, series in distributions_by_ticker.items()
            if ticker in distribution_yields
        )
        return {
            "status": "READY",
            "asOf": as_of.isoformat(),
            "projectionMethod": "HISTORICAL_TOTAL_RETURN_CAGR",
            "reinvestmentMode": "TOTAL_RETURN_REINVEST",
            "fx": snapshot["fx"],
            "calendar": {
                "name": self.trading_calendar.name,
                "version": self.trading_calendar.version,
            },
            "dataCoveragePercent": decimal_string(
                covered_value / total_value * Decimal("100") if total_value else Decimal()
            ),
            "dailyContributionKRW": decimal_string(
                sum((item.daily_contribution_krw for item in inputs), Decimal())
            ),
            "excludedTickers": sorted(excluded),
            "dataStatus": (
                "STALE"
                if any(item.stale for item in series_by_ticker.values())
                else "LIVE"
            ),
            "horizons": horizons,
            "wealthTimeline": chart_timeline,
            "milestones": milestones,
            "income": {
                "status": "READY" if distribution_yields else "UNAVAILABLE",
                "method": "TRAILING_12M_DISTRIBUTION_YIELD",
                "label": "배당/분배금",
                "reinvested": True,
                "dataStatus": "STALE" if income_stale else "LIVE",
                "dataCoveragePercent": decimal_string(
                    distribution_covered_value / total_value * Decimal("100")
                    if total_value
                    else Decimal()
                ),
                "current": self._serialize_income(
                    current_annual_income_usd, Decimal(fx_rate)
                ),
                "horizons": income_horizons,
                "includedTickers": sorted(distribution_yields),
                "excludedTickers": sorted(
                    set(tickers) - set(distribution_yields)
                ),
                "disclaimer": (
                    "최근 12개월 실제 배당/분배금의 현재가격 대비 yield가 유지된다는 "
                    "단순 참고 시나리오입니다. 지급액과 구성은 보장되지 않으며 Total Return에 "
                    "이미 반영된 분배금을 자산성장에 다시 더하지 않습니다."
                ),
            },
            "disclaimer": (
                "과거 배당·분할 조정 Total Return CAGR이 유지된다는 가정입니다. "
                "미래수익을 보장하지 않으며 원화 금액은 현재 환율 유지 가정입니다."
            ),
        }

    async def compare_projection(
        self,
        changes: dict[str, tuple[Decimal, bool]],
    ) -> dict[str, Any]:
        base = await self.get_projection()
        modified = await self.get_projection(contribution_overrides=changes)
        if base.get("status") != "READY" or modified.get("status") != "READY":
            return {
                "status": "UNAVAILABLE",
                "message": modified.get("message") or base.get("message"),
            }
        deltas = {}
        for horizon in ("1Y", "3Y", "5Y", "7Y", "10Y"):
            base_value = Decimal(base["horizons"][horizon]["endingValueKRW"])
            modified_value = Decimal(modified["horizons"][horizon]["endingValueKRW"])
            base_principal = Decimal(
                base["horizons"][horizon]["addedPrincipalKRW"]
            )
            modified_principal = Decimal(
                modified["horizons"][horizon]["addedPrincipalKRW"]
            )
            base_gain = Decimal(base["horizons"][horizon]["investmentGainKRW"])
            modified_gain = Decimal(
                modified["horizons"][horizon]["investmentGainKRW"]
            )
            base_income = Decimal(
                base["income"]["horizons"][horizon]["annualDistributionKRW"]
            )
            modified_income = Decimal(
                modified["income"]["horizons"][horizon]["annualDistributionKRW"]
            )
            deltas[horizon] = {
                "baseEndingValueKRW": decimal_string(base_value),
                "modifiedEndingValueKRW": decimal_string(modified_value),
                "deltaKRW": decimal_string(modified_value - base_value),
                "deltaPercent": decimal_string(
                    (modified_value / base_value - Decimal("1")) * Decimal("100")
                    if base_value
                    else None
                ),
                "addedPrincipalDeltaKRW": decimal_string(
                    modified_principal - base_principal
                ),
                "investmentGainDeltaKRW": decimal_string(modified_gain - base_gain),
                "annualDistributionDeltaKRW": decimal_string(
                    modified_income - base_income
                ),
            }
        return {
            "status": "READY",
            "baseDailyContributionKRW": base["dailyContributionKRW"],
            "modifiedDailyContributionKRW": modified["dailyContributionKRW"],
            "deltas": deltas,
            "modifiedProjection": modified,
            "disclaimer": modified["disclaimer"],
        }

    async def save_scenario(
        self,
        *,
        name: str,
        changes: dict[str, tuple[Decimal, bool]],
    ) -> dict[str, Any]:
        snapshot = await self.get_snapshot(refresh=False)
        items = {
            position["instrument"]["ticker"]: {
                "market": position["instrument"]["market"],
                "ticker": position["instrument"]["ticker"],
                "asset_type": position["instrument"]["assetType"],
                "instrument_currency": position["instrument"]["currency"],
                "configured_amount_krw": Decimal(
                    position["configuredContributionKRW"]
                ),
                "enabled": bool(position["dailyBuyEnabled"]),
            }
            for position in snapshot["positions"]
        }
        for ticker, (amount, enabled) in changes.items():
            if not _TICKER.fullmatch(ticker):
                raise ValueError("ticker is malformed")
            existing = items.get(ticker)
            if existing is None:
                existing = {
                    "market": "US",
                    "ticker": ticker,
                    "asset_type": "ETF",
                    "instrument_currency": "USD",
                }
                items[ticker] = existing
            existing["configured_amount_krw"] = amount
            existing["enabled"] = enabled
        scenario = await self.repository.save_investment_scenario(
            name=name,
            items=list(items.values()),
        )
        return self._serialize_saved_scenario(scenario)

    async def list_scenarios(self) -> dict[str, Any]:
        scenarios = await self.repository.list_investment_scenarios()
        return {
            "status": "READY",
            "scenarios": [self._serialize_saved_scenario(item) for item in scenarios],
        }

    async def compare_saved_scenario(self, scenario_id: int) -> dict[str, Any]:
        scenario = await self.repository.get_investment_scenario(scenario_id)
        if scenario is None:
            raise LookupError("saved scenario was not found")
        changes = {
            item.ticker: (item.configured_amount_krw, item.enabled)
            for item in scenario.items
        }
        comparison = await self.compare_projection(changes)
        return {
            **comparison,
            "scenario": self._serialize_saved_scenario(scenario),
        }

    async def delete_saved_scenario(self, scenario_id: int) -> None:
        if not await self.repository.delete_investment_scenario(scenario_id):
            raise LookupError("saved scenario was not found")

    @staticmethod
    def _serialize_saved_scenario(scenario: Any) -> dict[str, Any]:
        daily = sum(
            (
                item.configured_amount_krw
                for item in scenario.items
                if item.enabled
            ),
            Decimal(),
        )
        return {
            "id": scenario.scenario_id,
            "name": scenario.name,
            "dailyContributionKRW": decimal_string(daily),
            "annualContributionKRW": decimal_string(daily * Decimal("252")),
            "createdAt": scenario.created_at.isoformat(),
            "updatedAt": scenario.updated_at.isoformat(),
            "plans": [
                {
                    "market": item.market,
                    "ticker": item.ticker,
                    "assetType": item.asset_type,
                    "currency": item.instrument_currency,
                    "configuredAmountKRW": decimal_string(
                        item.configured_amount_krw
                    ),
                    "enabled": item.enabled,
                    "effectiveContributionKRW": decimal_string(
                        item.configured_amount_krw if item.enabled else Decimal()
                    ),
                }
                for item in scenario.items
            ],
        }

    async def get_monte_carlo_projection(
        self,
        *,
        runs: int = 1000,
        block_days: int = 20,
        seed: int = 20260912,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if self.market_data is None or self.trading_calendar is None:
            return {
                "status": "UNAVAILABLE",
                "message": "시장데이터 또는 미국 거래일 calendar가 연결되지 않았습니다.",
            }
        snapshot = await self.get_snapshot(refresh=False)
        fx_value = snapshot["fx"]["rate"]
        if fx_value is None:
            return {"status": "UNAVAILABLE", "message": "USD/KRW 환율이 없습니다."}
        holdings = [
            item
            for item in snapshot["positions"]
            if Decimal(item["currentMarketValueUSD"]) > 0
            or Decimal(item["effectiveContributionKRW"]) > 0
        ]
        tickers = tuple(item["instrument"]["ticker"] for item in holdings)
        series_by_ticker = await self.market_data.get_total_return_series(
            tickers, lookback_years=10, refresh=refresh
        )
        return_maps: dict[str, dict[date, Decimal]] = {}
        for ticker, series in series_by_ticker.items():
            if series.currency != "USD" or len(series.points) < 253:
                continue
            daily: dict[date, Decimal] = {}
            previous: Decimal | None = None
            for day, value in series.points:
                if previous is not None and previous > 0 and value > 0:
                    daily[day] = value / previous - Decimal("1")
                previous = value
            if len(daily) >= 252:
                return_maps[ticker] = daily
        if not return_maps:
            return {
                "status": "UNAVAILABLE",
                "message": "Block bootstrap에 필요한 1년 이상의 Total Return 데이터가 없습니다.",
            }
        common_dates = sorted(set.intersection(*(set(item) for item in return_maps.values())))
        if len(common_dates) < max(252, block_days):
            return {
                "status": "UNAVAILABLE",
                "message": "ETF 간 공통 Total Return 거래일이 1년 미만입니다.",
            }
        values_by_ticker = {
            item["instrument"]["ticker"]: item for item in holdings
        }
        inputs = tuple(
            BootstrapPositionInput(
                ticker=ticker,
                current_value_usd=Decimal(values_by_ticker[ticker]["currentMarketValueUSD"]),
                daily_contribution_krw=Decimal(
                    values_by_ticker[ticker]["effectiveContributionKRW"]
                ),
                aligned_daily_returns=tuple(
                    return_maps[ticker][day] for day in common_dates
                ),
            )
            for ticker in sorted(return_maps)
            if ticker in values_by_ticker
        )
        cache_key = (
            runs,
            block_days,
            seed,
            str(fx_value),
            tuple(
                (
                    item.ticker,
                    str(item.current_value_usd),
                    str(item.daily_contribution_krw),
                    series_by_ticker[item.ticker].as_of.isoformat(),
                    series_by_ticker[item.ticker].stale,
                )
                for item in inputs
            ),
        )
        if not refresh and cache_key in self._monte_carlo_cache:
            return self._monte_carlo_cache[cache_key]
        as_of = date.today()
        horizon_dates = {
            f"{years}Y": self._add_years(as_of, years)
            for years in (1, 3, 5, 7, 10)
        }
        engine = HistoricalBlockBootstrapEngine(self.trading_calendar)
        result = await asyncio.to_thread(
            functools.partial(
                engine.project,
                inputs,
                as_of=as_of,
                horizon_dates=horizon_dates,
                usd_krw=Decimal(fx_value),
                runs=runs,
                block_days=block_days,
                seed=seed,
            )
        )
        covered_value = sum((item.current_value_usd for item in inputs), Decimal())
        total_value = Decimal(snapshot["summary"]["totalMarketValueUSD"])
        serialized_horizons = {}
        fx_rate = Decimal(fx_value)
        for key, horizon in result["horizons"].items():
            serialized_horizons[key] = {
                "tradingDays": horizon["tradingDays"],
                "startingValueKRW": decimal_string(
                    horizon["startingValueUSD"] * fx_rate
                ),
                "addedPrincipalKRW": decimal_string(
                    horizon["addedPrincipalUSD"] * fx_rate
                ),
                "percentiles": {
                    label: {
                        "endingValueUSD": decimal_string(point["endingValueUSD"]),
                        "endingValueKRW": decimal_string(
                            point["endingValueUSD"] * fx_rate
                        ),
                        "investmentGainUSD": decimal_string(
                            point["investmentGainUSD"]
                        ),
                        "investmentGainKRW": decimal_string(
                            point["investmentGainUSD"] * fx_rate
                        ),
                    }
                    for label, point in horizon["percentiles"].items()
                },
            }
        warnings = []
        if len(common_dates) < 756:
            warnings.append("SHORT_COMMON_HISTORY")
        excluded = sorted(set(tickers) - {item.ticker for item in inputs})
        if excluded:
            warnings.append("TICKERS_EXCLUDED_FOR_HISTORY")
        response = {
            "status": "READY",
            "asOf": as_of.isoformat(),
            "projectionMethod": "HISTORICAL_CORRELATED_BLOCK_BOOTSTRAP",
            "runs": result["runs"],
            "blockDays": result["blockDays"],
            "seed": result["seed"],
            "historicalObservationCount": result["historicalObservationCount"],
            "historicalCoverageStart": common_dates[0].isoformat(),
            "historicalCoverageEnd": common_dates[-1].isoformat(),
            "dataCoveragePercent": decimal_string(
                covered_value / total_value * Decimal("100")
                if total_value
                else Decimal()
            ),
            "includedTickers": [item.ticker for item in inputs],
            "excludedTickers": excluded,
            "horizons": serialized_horizons,
            "warnings": warnings,
            "disclaimer": (
                "과거 동일 거래일의 ETF Total Return 벡터를 20거래일 블록으로 재표본한 "
                "확률적 시뮬레이션입니다. P50은 보장값이 아니며 미래 환율은 현재 수준 유지 가정입니다."
            ),
        }
        self._monte_carlo_cache = {cache_key: response}
        return response

    async def get_allocation(self) -> dict[str, Any]:
        snapshot, projection = await asyncio.gather(
            self.get_snapshot(refresh=False),
            self.get_projection(refresh=False),
        )
        if snapshot["dataState"] != "READY":
            return {"status": "UNAVAILABLE", "message": "포트폴리오 데이터가 없습니다."}
        if snapshot["fx"]["rate"] is None:
            return {"status": "UNAVAILABLE", "message": "USD/KRW 환율이 없습니다."}
        projected_rows = (
            projection.get("horizons", {}).get("10Y", {}).get("positions", [])
            if projection.get("status") == "READY"
            else []
        )
        projected_by_ticker = {
            row["ticker"]: Decimal(row["endingValueKRW"])
            for row in projected_rows
        }
        ticker_values = []
        current_total = sum(
            (Decimal(item["currentMarketValueKRW"]) for item in snapshot["positions"]),
            Decimal(),
        )
        projected_total = sum(projected_by_ticker.values(), Decimal())
        category_values: dict[str, list[Decimal]] = {}
        for item in snapshot["positions"]:
            ticker = item["instrument"]["ticker"]
            current = Decimal(item["currentMarketValueKRW"])
            projected = projected_by_ticker.get(ticker, Decimal())
            ticker_values.append(
                {
                    "ticker": ticker,
                    "category": item["category"],
                    "currentValueKRW": decimal_string(current),
                    "currentPercent": decimal_string(
                        current / current_total * Decimal("100")
                        if current_total
                        else Decimal()
                    ),
                    "projected10YValueKRW": decimal_string(projected),
                    "projected10YPercent": decimal_string(
                        projected / projected_total * Decimal("100")
                        if projected_total
                        else Decimal()
                    ),
                }
            )
            bucket = category_values.setdefault(
                item["category"], [Decimal(), Decimal()]
            )
            bucket[0] += current
            bucket[1] += projected
        categories = [
            {
                "category": category,
                "currentValueKRW": decimal_string(values[0]),
                "currentPercent": decimal_string(
                    values[0] / current_total * Decimal("100")
                    if current_total
                    else Decimal()
                ),
                "projected10YValueKRW": decimal_string(values[1]),
                "projected10YPercent": decimal_string(
                    values[1] / projected_total * Decimal("100")
                    if projected_total
                    else Decimal()
                ),
            }
            for category, values in category_values.items()
        ]
        ticker_values.sort(
            key=lambda item: Decimal(item["currentValueKRW"]), reverse=True
        )
        categories.sort(
            key=lambda item: Decimal(item["currentValueKRW"]), reverse=True
        )
        return {
            "status": "READY",
            "projectionStatus": projection.get("status"),
            "projectionMethod": projection.get("projectionMethod"),
            "dataCoveragePercent": projection.get("dataCoveragePercent"),
            "tickers": ticker_values,
            "categories": categories,
            "disclaimer": (
                "10년 비중은 Historical Total Return CAGR 및 현재 환율 유지 가정이며 "
                "미래 비중을 보장하지 않습니다. 데이터 미포함 종목은 10년 비중에서 제외됩니다."
            ),
        }

    async def get_etf_detail(self, ticker: str) -> dict[str, Any]:
        normalized = ticker.strip().upper()
        if not _TICKER.fullmatch(normalized):
            raise ValueError("ticker is malformed")
        snapshot = await self.get_snapshot(refresh=False)
        position = next(
            (
                item
                for item in snapshot["positions"]
                if item["instrument"]["ticker"] == normalized
            ),
            None,
        )
        if position is None:
            raise LookupError("ticker is not in the portfolio or investment plan")
        if self.market_data is None:
            return {
                "status": "READY",
                "position": position,
                "history": {"status": "UNAVAILABLE"},
                "projection": {"status": "UNAVAILABLE"},
            }
        series_map, distribution_map, projection = await asyncio.gather(
            self.market_data.get_total_return_series(
                (normalized,), lookback_years=10, refresh=False
            ),
            self.market_data.get_distribution_series(
                (normalized,), lookback_years=5, refresh=False
            ),
            self.get_projection(refresh=False),
        )
        series = series_map.get(normalized)
        distribution = distribution_map.get(normalized)
        history: dict[str, Any] = {"status": "UNAVAILABLE"}
        warnings: list[str] = []
        if series is not None and len(series.points) >= 2:
            first_day = series.points[0][0]
            last_day = series.points[-1][0]
            history_years = Decimal((last_day - first_day).days) / Decimal("365.2425")
            history = {
                "status": "READY",
                "seriesKind": series.series_kind,
                "provider": series.provider,
                "asOf": series.as_of.isoformat(),
                "stale": series.stale,
                "coverageStart": first_day.isoformat(),
                "coverageEnd": last_day.isoformat(),
                "availableHistoryYears": decimal_string(history_years),
                "return1YPercent": decimal_string(
                    self._period_return(series.points, last_day - timedelta(days=365))
                ),
                "cagr3YPercent": decimal_string(
                    self._period_cagr(series.points, last_day, 3)
                ),
                "cagr5YPercent": decimal_string(
                    self._period_cagr(series.points, last_day, 5)
                ),
                "cagr10YPercent": decimal_string(
                    self._period_cagr(series.points, last_day, 10)
                ),
                **self._risk_metrics(series.points),
            }
            if history_years < Decimal("3"):
                warnings.append("SHORT_HISTORY_LONG_TERM_EXTRAPOLATION")
            elif history_years < Decimal("5"):
                warnings.append("LIMITED_HISTORY")
            if series.stale:
                warnings.append("STALE_TOTAL_RETURN_DATA")
        current_total = Decimal(snapshot["summary"]["totalMarketValueUSD"])
        current_value = Decimal(position["currentMarketValueUSD"])
        distribution_payload: dict[str, Any] = {"status": "UNAVAILABLE", "points": []}
        trailing_yield = None
        if distribution is not None and position["latestCurrentPriceUSD"] is not None:
            cutoff = date.today() - timedelta(days=365)
            trailing_per_share = sum(
                (
                    amount
                    for ex_date, amount, _ in distribution.points
                    if cutoff < ex_date <= date.today()
                ),
                Decimal(),
            )
            price = Decimal(position["latestCurrentPriceUSD"])
            trailing_yield = trailing_per_share / price if price > 0 else None
            distribution_payload = {
                "status": "READY",
                "provider": distribution.provider,
                "asOf": distribution.as_of.isoformat(),
                "stale": distribution.stale,
                "trailing12MPerShareUSD": decimal_string(trailing_per_share),
                "trailingYieldPercent": decimal_string(
                    trailing_yield * Decimal("100") if trailing_yield is not None else None
                ),
                "points": [
                    {
                        "exDate": ex_date.isoformat(),
                        "amountPerShareUSD": decimal_string(amount),
                        "classification": classification,
                    }
                    for ex_date, amount, classification in distribution.points[-12:]
                ],
            }
        future = {}
        if projection.get("status") == "READY":
            for horizon, payload in projection["horizons"].items():
                row = next(
                    (
                        item
                        for item in payload["positions"]
                        if item["ticker"] == normalized
                    ),
                    None,
                )
                if row is not None:
                    future[horizon] = {
                        **row,
                        "annualDistributionUSD": decimal_string(
                            Decimal(row["endingValueUSD"]) * trailing_yield
                            if trailing_yield is not None
                            else None
                        ),
                        "annualDistributionKRW": decimal_string(
                            Decimal(row["endingValueKRW"]) * trailing_yield
                            if trailing_yield is not None
                            else None
                        ),
                    }
        return {
            "status": "READY",
            "ticker": normalized,
            "position": position,
            "portfolioWeightPercent": decimal_string(
                current_value / current_total * Decimal("100")
                if current_total
                else Decimal()
            ),
            "history": history,
            "distribution": distribution_payload,
            "projection": {
                "status": "READY" if future else "UNAVAILABLE",
                "horizons": future,
                "method": projection.get("projectionMethod"),
            },
            "warnings": warnings,
            "disclaimer": (
                "과거 adjusted Total Return과 trailing distribution을 분리해 표시합니다. "
                "미래 수익과 배당/분배금은 보장되지 않습니다."
            ),
        }

    @staticmethod
    def _period_return(
        points: tuple[tuple[date, Decimal], ...], cutoff: date
    ) -> Decimal | None:
        selected = [(day, value) for day, value in points if day >= cutoff]
        if len(selected) < 2 or selected[0][1] <= 0:
            return None
        return (selected[-1][1] / selected[0][1] - Decimal("1")) * Decimal("100")

    @classmethod
    def _period_cagr(
        cls,
        points: tuple[tuple[date, Decimal], ...],
        last_day: date,
        years: int,
    ) -> Decimal | None:
        cutoff = cls._add_years(last_day, -years)
        selected = [(day, value) for day, value in points if day >= cutoff]
        if len(selected) < 2 or selected[0][0] > cutoff + timedelta(days=45):
            return None
        elapsed = Decimal((selected[-1][0] - selected[0][0]).days) / Decimal("365.2425")
        if elapsed <= 0 or selected[0][1] <= 0 or selected[-1][1] <= 0:
            return None
        return (
            (selected[-1][1] / selected[0][1]).ln() / elapsed
        ).exp() * Decimal("100") - Decimal("100")

    @staticmethod
    def _risk_metrics(
        points: tuple[tuple[date, Decimal], ...]
    ) -> dict[str, str | None]:
        returns = [
            current / previous - Decimal("1")
            for (_, previous), (_, current) in zip(points, points[1:], strict=False)
            if previous > 0 and current > 0
        ]
        volatility = None
        if len(returns) >= 2:
            mean = sum(returns, Decimal()) / Decimal(len(returns))
            variance = sum(
                ((value - mean) ** 2 for value in returns), Decimal()
            ) / Decimal(len(returns) - 1)
            volatility = variance.sqrt() * Decimal("252").sqrt() * Decimal("100")
        peak = points[0][1]
        max_drawdown = Decimal()
        for _, value in points:
            peak = max(peak, value)
            max_drawdown = min(max_drawdown, value / peak - Decimal("1"))
        return {
            "annualizedVolatilityPercent": decimal_string(volatility),
            "maxDrawdownPercent": decimal_string(max_drawdown * Decimal("100")),
        }

    @staticmethod
    def _add_years(value: date, years: int) -> date:
        try:
            return value.replace(year=value.year + years)
        except ValueError:
            return value.replace(year=value.year + years, day=28)

    @staticmethod
    def _serialize_projection_result(
        result: dict[str, object], fx_rate: str
    ) -> dict[str, Any]:
        rate = Decimal(fx_rate)
        fields = (
            "startingValueUSD",
            "addedPrincipalUSD",
            "investmentGainUSD",
            "endingValueUSD",
        )
        serialized = {field: decimal_string(result[field]) for field in fields}
        serialized.update(
            {
                field.replace("USD", "KRW"): decimal_string(result[field] * rate)
                for field in fields
            }
        )
        serialized["tradingDays"] = result["tradingDays"]
        serialized["positions"] = [
            {
                **{
                    key: decimal_string(value) if isinstance(value, Decimal) else value
                    for key, value in row.items()
                },
                **{
                    key.replace("USD", "KRW"): decimal_string(value * rate)
                    for key, value in row.items()
                    if key.endswith("USD") and isinstance(value, Decimal)
                },
            }
            for row in result["positions"]
        ]
        return serialized

    @staticmethod
    def _serialize_timeline_point(
        point: dict[str, object], fx_rate: str
    ) -> dict[str, Any]:
        rate = Decimal(fx_rate)
        return {
            "date": point["date"].isoformat(),
            **{
                field.replace("USD", "KRW"): decimal_string(point[field] * rate)
                for field in (
                    "startingValueUSD",
                    "addedPrincipalUSD",
                    "investmentGainUSD",
                    "endingValueUSD",
                )
            },
        }

    @staticmethod
    def _serialize_income(
        annual_income_usd: Decimal, fx_rate: Decimal
    ) -> dict[str, str | None]:
        annual_krw = annual_income_usd * fx_rate
        return {
            "annualDistributionUSD": decimal_string(annual_income_usd),
            "annualDistributionKRW": decimal_string(annual_krw),
            "monthlyAverageDistributionUSD": decimal_string(
                annual_income_usd / Decimal("12")
            ),
            "monthlyAverageDistributionKRW": decimal_string(
                annual_krw / Decimal("12")
            ),
        }
