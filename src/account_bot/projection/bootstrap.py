"""Historical correlated block-bootstrap projection using Decimal balances."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .calendar import TradingCalendarProvider


@dataclass(frozen=True, slots=True)
class BootstrapPositionInput:
    ticker: str
    current_value_usd: Decimal
    daily_contribution_krw: Decimal
    aligned_daily_returns: tuple[Decimal, ...]


class HistoricalBlockBootstrapEngine:
    def __init__(self, calendar: TradingCalendarProvider) -> None:
        self.calendar = calendar

    def project(
        self,
        positions: tuple[BootstrapPositionInput, ...],
        *,
        as_of: date,
        horizon_dates: dict[str, date],
        usd_krw: Decimal,
        runs: int = 1000,
        block_days: int = 20,
        seed: int = 20260912,
    ) -> dict[str, object]:
        if usd_krw <= 0:
            raise ValueError("usd_krw must be positive")
        if not 1 <= runs <= 5000:
            raise ValueError("runs must be between 1 and 5000")
        if block_days <= 0:
            raise ValueError("block_days must be positive")
        if not positions or not horizon_dates:
            raise ValueError("positions and horizon_dates are required")
        observation_count = len(positions[0].aligned_daily_returns)
        if observation_count < block_days:
            raise ValueError("historical return coverage is shorter than one block")
        if any(len(item.aligned_daily_returns) != observation_count for item in positions):
            raise ValueError("daily return series must be aligned")

        max_end = max(horizon_dates.values())
        sessions = self.calendar.get_trading_days(as_of, max_end)
        horizon_counts = {
            key: sum(session <= end for session in sessions)
            for key, end in horizon_dates.items()
        }
        horizons_at_count: dict[int, list[str]] = {}
        for key, count in horizon_counts.items():
            horizons_at_count.setdefault(count, []).append(key)

        starting = sum((item.current_value_usd for item in positions), Decimal())
        contributions = tuple(
            item.daily_contribution_krw / usd_krw for item in positions
        )
        total_daily_contribution = sum(contributions, Decimal())
        samples: dict[str, list[Decimal]] = {key: [] for key in horizon_dates}
        rng = random.Random(seed)
        max_block_start = observation_count - block_days

        for _ in range(runs):
            balances = [item.current_value_usd for item in positions]
            source_index = 0
            remaining_in_block = 0
            for future_index in range(1, len(sessions) + 1):
                if remaining_in_block == 0:
                    source_index = rng.randrange(max_block_start + 1)
                    remaining_in_block = block_days
                for position_index, item in enumerate(positions):
                    balances[position_index] = balances[position_index] * (
                        Decimal("1") + item.aligned_daily_returns[source_index]
                    ) + contributions[position_index]
                source_index += 1
                remaining_in_block -= 1
                for key in horizons_at_count.get(future_index, ()):
                    samples[key].append(sum(balances, Decimal()))

        percentile_levels = (10, 25, 50, 75, 90)
        horizon_results = {}
        for key, values in samples.items():
            ordered = sorted(values)
            trading_days = horizon_counts[key]
            added = total_daily_contribution * Decimal(trading_days)
            horizon_results[key] = {
                "tradingDays": trading_days,
                "startingValueUSD": starting,
                "addedPrincipalUSD": added,
                "percentiles": {
                    f"P{level}": {
                        "endingValueUSD": self._percentile(ordered, level),
                        "investmentGainUSD": self._percentile(ordered, level)
                        - starting
                        - added,
                    }
                    for level in percentile_levels
                },
            }
        return {
            "runs": runs,
            "blockDays": block_days,
            "seed": seed,
            "historicalObservationCount": observation_count,
            "horizons": horizon_results,
        }

    @staticmethod
    def _percentile(ordered: list[Decimal], level: int) -> Decimal:
        if not ordered:
            raise ValueError("percentile requires samples")
        index = (len(ordered) - 1) * level // 100
        return ordered[index]
