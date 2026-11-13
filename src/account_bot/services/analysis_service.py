"""Deterministic quantitative analysis plus conservative narrative context."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from account_bot.analysis import (
    CompanyScoreInput,
    FinalScoreResult,
    HardWarning,
    PortfolioScoreInput,
    ScoreResult,
    ScoringWeights,
    buy_capacity_score,
    calculate_company_score,
    calculate_portfolio_score,
    calculate_valuation_decision,
    concentration_capacity_score,
)
from account_bot.domain import InstrumentKey, instruments_compatible
from account_bot.market import (
    EarningsData,
    FundamentalData,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    TrendData,
    ValuationData,
)
from account_bot.market.news import assess_news
from account_bot.portfolio import PortfolioSnapshot


def _clamp(value: Decimal) -> Decimal:
    return min(Decimal("100"), max(Decimal("0"), value))


def _linear(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal | None:
    if value is None:
        return None
    if high <= low:
        raise ValueError("invalid normalization range")
    return _clamp((value - low) / (high - low) * Decimal("100"))


def _mean(values: list[Decimal | None]) -> Decimal | None:
    available = [value for value in values if value is not None]
    return sum(available, Decimal("0")) / len(available) if available else None


def fundamental_component(data: FundamentalData) -> Decimal | None:
    debt_safety: Decimal | None = None
    if data.total_cash is not None and data.total_debt is not None:
        if data.total_debt <= 0:
            debt_safety = Decimal("100")
        else:
            debt_safety = _clamp(data.total_cash / data.total_debt * Decimal("50"))
    fcf_score = (
        None
        if data.free_cash_flow is None
        else (Decimal("80") if data.free_cash_flow > 0 else Decimal("20"))
    )
    return _mean(
        [
            _linear(data.revenue_growth, Decimal("-0.10"), Decimal("0.30")),
            _linear(data.earnings_growth, Decimal("-0.20"), Decimal("0.40")),
            _linear(data.gross_margin, Decimal("0.10"), Decimal("0.70")),
            _linear(data.operating_margin, Decimal("0"), Decimal("0.35")),
            _linear(data.return_on_equity, Decimal("0"), Decimal("0.30")),
            fcf_score,
            debt_safety,
        ]
    )


def growth_component(data: FundamentalData) -> Decimal | None:
    return _mean(
        [
            _linear(data.revenue_growth, Decimal("-0.05"), Decimal("0.35")),
            _linear(data.earnings_growth, Decimal("-0.10"), Decimal("0.50")),
        ]
    )


def valuation_component(data: ValuationData) -> Decimal | None:
    metrics: list[Decimal | None] = []
    if data.forward_pe is not None:
        metrics.append(Decimal("100") - _linear(data.forward_pe, Decimal("10"), Decimal("60")))
    if data.peg is not None:
        metrics.append(Decimal("100") - _linear(data.peg, Decimal("0.5"), Decimal("3")))
    if data.enterprise_to_ebitda is not None:
        metrics.append(
            Decimal("100") - _linear(data.enterprise_to_ebitda, Decimal("5"), Decimal("35"))
        )
    if data.free_cash_flow_yield is not None:
        metrics.append(_linear(data.free_cash_flow_yield, Decimal("0"), Decimal("0.08")))
    if data.forward_pe is not None and data.historical_forward_pe_median not in (None, 0):
        ratio = data.forward_pe / data.historical_forward_pe_median
        metrics.append(Decimal("100") - _linear(ratio, Decimal("0.7"), Decimal("1.4")))
    if data.dilution_pct is not None:
        metrics.append(
            Decimal("100")
            - _linear(data.dilution_pct, Decimal("0"), Decimal("10"))
        )
    return _mean(metrics)


def safety_component(data: FundamentalData) -> Decimal | None:
    metrics: list[Decimal | None] = []
    if data.free_cash_flow is not None:
        metrics.append(Decimal("85") if data.free_cash_flow > 0 else Decimal("15"))
    if data.total_cash is not None and data.total_debt is not None:
        metrics.append(
            Decimal("100")
            if data.total_debt <= 0
            else _clamp(data.total_cash / data.total_debt * Decimal("60"))
        )
    if data.debt_to_equity is not None:
        metrics.append(Decimal("100") - _linear(data.debt_to_equity, Decimal("20"), Decimal("250")))
    return _mean(metrics)


@dataclass(frozen=True, slots=True)
class RecurringSuitabilityResult:
    score: Decimal | None
    reasons: tuple[str, ...]


def recurring_suitability(trend: TrendData | None) -> RecurringSuitabilityResult:
    if trend is None:
        return RecurringSuitabilityResult(None, ("장기 가격 이력 확인 불가",))
    metrics: list[tuple[str, Decimal]] = []
    if trend.cagr_3y is not None:
        metrics.append(
            ("3년 CAGR", _linear(trend.cagr_3y, Decimal("-0.05"), Decimal("0.15")))
        )
    if trend.return_1y is not None:
        metrics.append(
            ("1년 수익률", _linear(trend.return_1y, Decimal("-0.20"), Decimal("0.20")))
        )
    if trend.max_drawdown_3y is not None:
        metrics.append(
            (
                "3년 최대낙폭",
                _linear(trend.max_drawdown_3y, Decimal("-0.60"), Decimal("-0.15")),
            )
        )
    if trend.annualized_volatility_1y is not None:
        volatility_score = Decimal("100") - _linear(
            trend.annualized_volatility_1y, Decimal("0.15"), Decimal("0.50")
        )
        metrics.append(("1년 변동성", volatility_score))
    if trend.above_200d_average is not None:
        metrics.append(
            ("200일 추세", Decimal("100") if trend.above_200d_average else Decimal("30"))
        )
    if len(metrics) < 3:
        return RecurringSuitabilityResult(None, ("장기추세 지표 커버리지 부족",))
    score = sum((value for _, value in metrics), Decimal("0")) / Decimal(len(metrics))
    reasons = (
        f"3년 CAGR {trend.cagr_3y * 100:+.2f}%" if trend.cagr_3y is not None else "",
        (
            f"최대낙폭 {trend.max_drawdown_3y * 100:.2f}%"
            if trend.max_drawdown_3y is not None
            else ""
        ),
        "200일선 상회" if trend.above_200d_average else "200일선 하회",
    )
    return RecurringSuitabilityResult(
        score=score.quantize(Decimal("0.01")),
        reasons=tuple(reason for reason in reasons if reason),
    )


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    market: str
    ticker: str
    price: PriceQuote | None
    fundamentals: FundamentalData | None
    valuation: ValuationData | None
    earnings: EarningsData | None
    news: tuple[NewsItem, ...]
    trend: TrendData | None
    recurring_suitability: RecurringSuitabilityResult
    company_score: ScoreResult
    portfolio_score: ScoreResult
    final_score: FinalScoreResult
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


class CompanyAnalysisService:
    def __init__(
        self,
        market_provider: MarketDataProvider,
        *,
        weights: ScoringWeights | None = None,
    ) -> None:
        self.provider = market_provider
        self.weights = weights or ScoringWeights()

    @staticmethod
    async def _optional(awaitable):
        try:
            return await awaitable, None
        except Exception as exc:
            return None, type(exc).__name__

    async def analyze(
        self,
        *,
        market: str,
        ticker: str,
        portfolio: PortfolioSnapshot,
        target_weight_pct: Decimal | None,
        hard_warnings: tuple[HardWarning, ...] = (),
    ) -> AnalysisResult:
        trend_loader = getattr(self.provider, "get_trend", None)
        trend_pair_awaitable = (
            self._optional(trend_loader(market, ticker))
            if callable(trend_loader)
            else asyncio.sleep(0, result=(None, "NotSupported"))
        )
        (
            price_pair,
            fundamentals_pair,
            valuation_pair,
            earnings_pair,
            news_pair,
            trend_pair,
        ) = await asyncio.gather(
            self._optional(self.provider.get_price(market, ticker)),
            self._optional(self.provider.get_fundamentals(market, ticker)),
            self._optional(self.provider.get_valuation(market, ticker)),
            self._optional(self.provider.get_earnings(market, ticker)),
            self._optional(self.provider.get_news(market, ticker, limit=10)),
            trend_pair_awaitable,
        )
        price, price_error = price_pair
        fundamentals, fundamental_error = fundamentals_pair
        valuation, valuation_error = valuation_pair
        earnings, earnings_error = earnings_pair
        news, news_error = news_pair
        trend, trend_error = trend_pair
        assessed_news = tuple(assess_news(news or []))

        valuation_score = valuation_component(valuation) if valuation else None
        company = calculate_company_score(
            CompanyScoreInput(
                fundamental=fundamental_component(fundamentals) if fundamentals else None,
                valuation=valuation_score,
                growth=growth_component(fundamentals) if fundamentals else None,
                moat=None,  # Requires durable competitive evidence/manual review.
                safety=safety_component(fundamentals) if fundamentals else None,
            ),
            weights=self.weights,
        )

        matched = next(
            (
                position
                for position in portfolio.positions
                if instruments_compatible(
                    position.instrument,
                    InstrumentKey(
                        market,
                        ticker,
                        position.instrument.asset_type,
                        position.instrument.currency,
                    ),
                )
            ),
            None,
        )
        current_weight = (
            matched.weight_pct
            if matched
            else (Decimal("0") if portfolio.weights_available else None)
        )
        target_alignment = (
            buy_capacity_score(current_weight, target_weight_pct)
            if current_weight is not None and target_weight_pct is not None
            else None
        )
        single_capacity = (
            concentration_capacity_score(
                current_weight, max(target_weight_pct or Decimal("10"), Decimal("10"))
            )
            if current_weight is not None
            else None
        )
        currency_exposure = None
        if portfolio.weights_available and matched is not None:
            currency_exposure = sum(
                (position.weight_pct or Decimal("0"))
                for position in portfolio.positions
                if position.instrument.currency == matched.instrument.currency
            )
        currency_capacity = (
            concentration_capacity_score(currency_exposure, Decimal("80"))
            if currency_exposure is not None
            else None
        )
        portfolio_score = calculate_portfolio_score(
            PortfolioScoreInput(
                portfolio_complete=portfolio.is_complete,
                target_alignment=target_alignment,
                single_stock_capacity=single_capacity,
                sector_capacity=None,
                currency_capacity=currency_capacity,
                correlation=None,
            ),
            weights=self.weights,
        )
        final = calculate_valuation_decision(
            valuation_score,
            hard_warnings=hard_warnings,
        )

        reasons: list[str] = []
        if fundamentals and fundamentals.revenue_growth is not None:
            reasons.append(
                f"매출 성장률 {fundamentals.revenue_growth * Decimal('100'):.1f}%"
                f"({fundamentals.provider}, 기준 {fundamentals.as_of.date()})"
            )
        if valuation and valuation.forward_pe is not None:
            reasons.append(
                f"Forward PER {valuation.forward_pe:.2f}배를 성장·현금흐름 지표와 함께 평가함"
            )
        if current_weight is not None and target_weight_pct is not None:
            relationship = "이하" if current_weight <= target_weight_pct else "초과"
            reasons.append(
                f"현재 비중 {current_weight:.2f}%가 목표 {target_weight_pct:.2f}% {relationship}"
            )
        if not reasons:
            reasons.append("판단에 필요한 정량 데이터가 충분하지 않음")
        if len(reasons) < 3:
            company_value = (
                f"{company.score:.2f}점" if company.score is not None else "커버리지 미달로 보류"
            )
            reasons.append(
                f"기업 점수 {company_value}(정량 데이터 커버리지 {company.coverage_pct:.2f}%)"
            )
        if len(reasons) < 3:
            portfolio_value = (
                f"{portfolio_score.score:.2f}점"
                if portfolio_score.score is not None
                else "불완전 데이터로 보류"
            )
            reasons.append(
                f"포트폴리오 점수 {portfolio_value}"
                f"(스냅샷 {'완전' if portfolio.is_complete else '부분'})"
            )
        warnings = tuple(
            f"{name}:{error}"
            for name, error in (
                ("price", price_error),
                ("fundamental", fundamental_error),
                ("valuation", valuation_error),
                ("earnings", earnings_error),
                ("news", news_error),
                ("trend", trend_error),
            )
            if error
        )
        return AnalysisResult(
            market=market.upper(),
            ticker=ticker.upper(),
            price=price,
            fundamentals=fundamentals,
            valuation=valuation,
            earnings=earnings,
            news=assessed_news,
            trend=trend,
            recurring_suitability=recurring_suitability(trend),
            company_score=company,
            portfolio_score=portfolio_score,
            final_score=final,
            reasons=tuple(reasons[:3]),
            warnings=warnings,
        )
