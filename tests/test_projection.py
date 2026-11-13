from __future__ import annotations

from datetime import date
from decimal import Decimal

from account_bot.projection import (
    DeterministicProjectionEngine,
    ProjectionInput,
    TradingCalendarProvider,
)


class _TenDayCalendar(TradingCalendarProvider):
    name = "test-calendar"
    version = "1"

    def count_trading_days(self, start: date, end: date) -> int:
        del start, end
        return 10

    def get_trading_days(self, start: date, end: date) -> tuple[date, ...]:
        del end
        return tuple(date.fromordinal(start.toordinal() + day) for day in range(1, 11))


def test_zero_return_projection_includes_each_trading_day_contribution() -> None:
    engine = DeterministicProjectionEngine(_TenDayCalendar())
    result = engine.project(
        (
            ProjectionInput(
                ticker="NEW",
                current_value_usd=Decimal("0"),
                daily_contribution_krw=Decimal("1400"),
                annual_return=Decimal("0"),
                history_years=Decimal("5"),
            ),
        ),
        as_of=date(2026, 1, 1),
        horizon_date=date(2027, 1, 1),
        usd_krw=Decimal("1400"),
    )
    assert result["tradingDays"] == 10
    assert result["addedPrincipalUSD"] == Decimal("10")
    assert result["investmentGainUSD"] == Decimal("0")
    assert result["endingValueUSD"] == Decimal("10")


def test_negative_return_and_existing_holding_remain_decimal() -> None:
    engine = DeterministicProjectionEngine(_TenDayCalendar())
    result = engine.project(
        (
            ProjectionInput(
                ticker="ETF",
                current_value_usd=Decimal("100"),
                daily_contribution_krw=Decimal("0"),
                annual_return=Decimal("-0.10"),
                history_years=Decimal("2.5"),
            ),
        ),
        as_of=date(2026, 1, 1),
        horizon_date=date(2027, 1, 1),
        usd_krw=Decimal("1400"),
    )
    row = result["positions"][0]
    assert isinstance(result["endingValueUSD"], Decimal)
    assert Decimal("0") < result["endingValueUSD"] < Decimal("100")
    assert row["warnings"] == ["SHORT_HISTORY_LONG_TERM_EXTRAPOLATION"]


def test_timeline_applies_each_trading_day_contribution_and_keeps_decimal() -> None:
    engine = DeterministicProjectionEngine(_TenDayCalendar(), trading_days_per_year=10)
    points = engine.project_timeline(
        (
            ProjectionInput(
                ticker="ZERO",
                current_value_usd=Decimal("100"),
                daily_contribution_krw=Decimal("1400"),
                annual_return=Decimal("0"),
                history_years=Decimal("10"),
            ),
        ),
        as_of=date(2026, 1, 1),
        horizon_date=date(2026, 12, 31),
        usd_krw=Decimal("1400"),
        sample_monthly=False,
    )

    assert len(points) == 11
    assert points[-1]["endingValueUSD"] == Decimal("110")
    assert points[-1]["addedPrincipalUSD"] == Decimal("10")
    assert points[-1]["investmentGainUSD"] == Decimal("0")
    assert isinstance(points[-1]["endingValueUSD"], Decimal)
