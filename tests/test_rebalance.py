from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.analysis import (  # noqa: E402
    RebalanceAction,
    TargetAllocation,
    suggest_new_money_rebalance,
)
from account_bot.domain import (  # noqa: E402
    BrokerError,
    BrokerResult,
    BrokerStatus,
    Currency,
    InstrumentKey,
    Money,
    Position,
)
from account_bot.portfolio import PortfolioAggregator  # noqa: E402


class RebalanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = InstrumentKey("NASDAQ", "AAA", currency=Currency.USD)
        self.b = InstrumentKey("NYSE", "BBB", currency=Currency.USD)

    def snapshot(self):
        return PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok(
                    "kiwoom",
                    [
                        Position("kiwoom", "one", self.a, "1", cost_basis="50", market_value="70"),
                        Position("kiwoom", "one", self.b, "1", cost_basis="20", market_value="30"),
                    ],
                ),
            ),
            expected_brokers=("kiwoom",),
        )

    def test_new_money_goes_to_underweight_asset_and_never_suggests_sale(self) -> None:
        plan = suggest_new_money_rebalance(
            self.snapshot(),
            (TargetAllocation(self.a, "50"), TargetAllocation(self.b, "50")),
            Money("20", Currency.USD),
        )
        self.assertTrue(plan.is_eligible)
        self.assertFalse(plan.uses_sales)
        self.assertEqual(plan.allocated_new_money.amount, Decimal("20"))
        self.assertEqual(plan.unallocated_cash.amount, Decimal("0"))
        by_key = {item.instrument: item for item in plan.items}
        self.assertEqual(by_key[self.a].suggested_new_money.amount, Decimal("0"))
        self.assertEqual(by_key[self.a].action, RebalanceAction.OVERWEIGHT_NO_NEW_MONEY)
        self.assertEqual(by_key[self.b].suggested_new_money.amount, Decimal("20"))
        self.assertEqual(by_key[self.b].action, RebalanceAction.ADD_WITH_NEW_MONEY)
        self.assertEqual(plan.projected_total_value, Money("120", Currency.USD))

    def test_cash_remains_when_security_targets_sum_below_one_hundred(self) -> None:
        plan = suggest_new_money_rebalance(
            self.snapshot(),
            (TargetAllocation(self.a, "40"), TargetAllocation(self.b, "40")),
            Money("100", Currency.USD),
        )
        # Projected total is 200; both target values are 80. Deficits are 10+50.
        self.assertEqual(plan.allocated_new_money.amount, Decimal("60"))
        self.assertEqual(plan.unallocated_cash.amount, Decimal("40"))
        self.assertFalse(plan.uses_sales)

    def test_partial_snapshot_withholds_rebalance(self) -> None:
        first = Position("kiwoom", "one", self.a, "1", market_value="70")
        partial_snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok("kiwoom", [first]),
                BrokerResult.failure(
                    "toss",
                    BrokerStatus.UNAVAILABLE,
                    error=BrokerError("timeout", "broker unavailable", retryable=True),
                ),
            ),
            expected_brokers=("kiwoom", "toss"),
        )
        plan = suggest_new_money_rebalance(
            partial_snapshot,
            (TargetAllocation(self.a, "100"),),
            Money("20", Currency.USD),
        )
        self.assertFalse(plan.is_eligible)
        self.assertEqual(plan.items, ())
        self.assertEqual(plan.allocated_new_money.amount, Decimal("0"))
        self.assertEqual(plan.unallocated_cash.amount, Decimal("20"))
        self.assertFalse(plan.uses_sales)

    def test_exchange_target_resolves_to_single_generic_us_holding(self) -> None:
        generic_us = InstrumentKey("US", "AAA", currency=Currency.USD)
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok(
                    "toss",
                    [
                        Position(
                            "toss",
                            "one",
                            generic_us,
                            "1",
                            cost_basis="80",
                            market_value="100",
                        )
                    ],
                ),
            ),
            expected_brokers=("toss",),
        )

        plan = suggest_new_money_rebalance(
            snapshot,
            (TargetAllocation(self.a, "100"),),
            Money("20", Currency.USD),
        )

        self.assertTrue(plan.is_eligible)
        self.assertEqual(len(plan.items), 1)
        self.assertEqual(plan.items[0].instrument, generic_us)
        self.assertEqual(plan.items[0].suggested_new_money.amount, Decimal("20"))
        self.assertIn("instrument_alias_resolved:NASDAQ:AAA->US:AAA", plan.warnings)

    def test_ambiguous_generic_us_target_withholds_rebalance(self) -> None:
        generic_target = InstrumentKey("US", "AAA", currency=Currency.USD)
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok(
                    "kiwoom",
                    [
                        Position("kiwoom", "one", self.a, "1", market_value="70"),
                        Position(
                            "kiwoom",
                            "one",
                            InstrumentKey("NYSE", "AAA", currency=Currency.USD),
                            "1",
                            market_value="30",
                        ),
                    ],
                ),
            ),
            expected_brokers=("kiwoom",),
        )

        plan = suggest_new_money_rebalance(
            snapshot,
            (TargetAllocation(generic_target, "100"),),
            Money("20", Currency.USD),
        )

        self.assertFalse(plan.is_eligible)
        self.assertEqual(plan.items, ())
        self.assertEqual(plan.unallocated_cash.amount, Decimal("20"))
        self.assertIn("rebalance_withheld:ambiguous_instrument_alias", plan.warnings)

    def test_invalid_target_sum_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            suggest_new_money_rebalance(
                self.snapshot(),
                (TargetAllocation(self.a, "60"), TargetAllocation(self.b, "50")),
                Money("20", Currency.USD),
            )


if __name__ == "__main__":
    unittest.main()
