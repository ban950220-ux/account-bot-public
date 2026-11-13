from __future__ import annotations

from datetime import date
from decimal import Decimal

from account_bot.projection import (
    BootstrapPositionInput,
    HistoricalBlockBootstrapEngine,
    TradingCalendarProvider,
)


class _Calendar(TradingCalendarProvider):
    name = "test"
    version = "1"

    def get_trading_days(self, start: date, end: date) -> tuple[date, ...]:
        del end
        return tuple(date.fromordinal(start.toordinal() + index) for index in range(1, 11))

    def count_trading_days(self, start: date, end: date) -> int:
        return len(self.get_trading_days(start, end))


def test_zero_return_bootstrap_includes_contributions() -> None:
    engine = HistoricalBlockBootstrapEngine(_Calendar())
    result = engine.project(
        (
            BootstrapPositionInput(
                "AAA",
                Decimal("100"),
                Decimal("1400"),
                (Decimal("0"),) * 20,
            ),
        ),
        as_of=date(2026, 1, 1),
        horizon_dates={"1Y": date(2027, 1, 1)},
        usd_krw=Decimal("1400"),
        runs=25,
        block_days=5,
        seed=7,
    )

    horizon = result["horizons"]["1Y"]
    assert horizon["addedPrincipalUSD"] == Decimal("10")
    assert horizon["percentiles"]["P50"]["endingValueUSD"] == Decimal("110")
    assert horizon["percentiles"]["P50"]["investmentGainUSD"] == Decimal("0")


def test_fixed_seed_is_reproducible_and_preserves_decimal() -> None:
    engine = HistoricalBlockBootstrapEngine(_Calendar())
    inputs = (
        BootstrapPositionInput(
            "AAA",
            Decimal("100"),
            Decimal("0"),
            tuple(Decimal(value) for value in ("0.01", "-0.01", "0.02", "-0.02") * 5),
        ),
        BootstrapPositionInput(
            "BBB",
            Decimal("100"),
            Decimal("0"),
            tuple(Decimal(value) for value in ("0.02", "-0.02", "0.04", "-0.04") * 5),
        ),
    )
    kwargs = {
        "as_of": date(2026, 1, 1),
        "horizon_dates": {"1Y": date(2027, 1, 1)},
        "usd_krw": Decimal("1400"),
        "runs": 40,
        "block_days": 4,
        "seed": 99,
    }

    first = engine.project(inputs, **kwargs)
    second = engine.project(inputs, **kwargs)

    assert first == second
    median = first["horizons"]["1Y"]["percentiles"]["P50"]["endingValueUSD"]
    assert isinstance(median, Decimal)
