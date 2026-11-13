"""Deterministic Total Return projection using Decimal throughout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .calendar import TradingCalendarProvider


@dataclass(frozen=True, slots=True)
class ProjectionInput:
    ticker: str
    current_value_usd: Decimal
    daily_contribution_krw: Decimal
    annual_return: Decimal
    history_years: Decimal


class DeterministicProjectionEngine:
    def __init__(
        self,
        calendar: TradingCalendarProvider,
        *,
        trading_days_per_year: int = 252,
    ) -> None:
        if trading_days_per_year <= 0:
            raise ValueError("trading_days_per_year must be positive")
        self.calendar = calendar
        self.trading_days_per_year = trading_days_per_year

    def project(
        self,
        positions: tuple[ProjectionInput, ...],
        *,
        as_of: date,
        horizon_date: date,
        usd_krw: Decimal,
    ) -> dict[str, object]:
        if usd_krw <= 0:
            raise ValueError("usd_krw must be positive")
        trading_days = self.calendar.count_trading_days(as_of, horizon_date)
        rows: list[dict[str, object]] = []
        for item in positions:
            if item.annual_return <= Decimal("-1"):
                raise ValueError("annual_return must be greater than -1")
            daily_rate = (
                ((Decimal("1") + item.annual_return).ln() / Decimal(self.trading_days_per_year)).exp()
                - Decimal("1")
            )
            growth_factor = (Decimal("1") + daily_rate) ** trading_days
            contribution_usd = item.daily_contribution_krw / usd_krw
            added_principal = contribution_usd * Decimal(trading_days)
            contribution_fv = (
                added_principal
                if daily_rate == 0
                else contribution_usd * (growth_factor - Decimal("1")) / daily_rate
            )
            existing_fv = item.current_value_usd * growth_factor
            ending_value = existing_fv + contribution_fv
            investment_gain = ending_value - item.current_value_usd - added_principal
            warnings = []
            if item.history_years < Decimal("3"):
                warnings.append("SHORT_HISTORY_LONG_TERM_EXTRAPOLATION")
            elif item.history_years < Decimal("5"):
                warnings.append("LIMITED_HISTORY")
            rows.append(
                {
                    "ticker": item.ticker,
                    "startingValueUSD": item.current_value_usd,
                    "addedPrincipalUSD": added_principal,
                    "investmentGainUSD": investment_gain,
                    "endingValueUSD": ending_value,
                    "annualReturnPercent": item.annual_return * Decimal("100"),
                    "historyYears": item.history_years,
                    "warnings": warnings,
                }
            )
        return {
            "tradingDays": trading_days,
            "positions": rows,
            "startingValueUSD": sum(
                (row["startingValueUSD"] for row in rows), Decimal()
            ),
            "addedPrincipalUSD": sum(
                (row["addedPrincipalUSD"] for row in rows), Decimal()
            ),
            "investmentGainUSD": sum(
                (row["investmentGainUSD"] for row in rows), Decimal()
            ),
            "endingValueUSD": sum((row["endingValueUSD"] for row in rows), Decimal()),
        }

    def project_timeline(
        self,
        positions: tuple[ProjectionInput, ...],
        *,
        as_of: date,
        horizon_date: date,
        usd_krw: Decimal,
        sample_monthly: bool = True,
    ) -> tuple[dict[str, object], ...]:
        """Simulate actual sessions and return contribution/gain snapshots.

        Returns are applied before that session's contribution. The full daily
        stream is used for milestone detection; callers may request compact
        month-end samples for chart transport.
        """
        if usd_krw <= 0:
            raise ValueError("usd_krw must be positive")
        sessions = self.calendar.get_trading_days(as_of, horizon_date)
        balances: dict[str, Decimal] = {}
        daily_rates: dict[str, Decimal] = {}
        contributions: dict[str, Decimal] = {}
        starting = Decimal()
        for item in positions:
            if item.annual_return <= Decimal("-1"):
                raise ValueError("annual_return must be greater than -1")
            balances[item.ticker] = item.current_value_usd
            daily_rates[item.ticker] = (
                ((Decimal("1") + item.annual_return).ln()
                 / Decimal(self.trading_days_per_year)).exp()
                - Decimal("1")
            )
            contributions[item.ticker] = item.daily_contribution_krw / usd_krw
            starting += item.current_value_usd

        snapshots: list[dict[str, object]] = []
        added = Decimal()

        def append_snapshot(day: date) -> None:
            ending = sum(balances.values(), Decimal())
            snapshots.append(
                {
                    "date": day,
                    "startingValueUSD": starting,
                    "addedPrincipalUSD": added,
                    "investmentGainUSD": ending - starting - added,
                    "endingValueUSD": ending,
                }
            )

        append_snapshot(as_of)
        for index, session in enumerate(sessions):
            for ticker in balances:
                contribution = contributions[ticker]
                balances[ticker] = balances[ticker] * (
                    Decimal("1") + daily_rates[ticker]
                ) + contribution
                added += contribution
            is_last = index == len(sessions) - 1
            is_month_end = is_last or sessions[index + 1].month != session.month
            if not sample_monthly or is_month_end:
                append_snapshot(session)
        return tuple(snapshots)
