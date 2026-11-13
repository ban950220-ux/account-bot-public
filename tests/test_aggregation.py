from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.domain import (  # noqa: E402
    AssetType,
    BrokerError,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    FxQuote,
    InstrumentKey,
    Money,
    Position,
    Transaction,
    TransactionSide,
)
from account_bot.portfolio import (  # noqa: E402
    PortfolioAggregator,
    RecurringBuyPlan,
    RecurringBuyStatus,
    evaluate_recurring_buy,
)


class AggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.amd = InstrumentKey("nasdaq", "amd", AssetType.EQUITY, Currency.USD)

    def test_instrument_identity_includes_listing_type_and_currency(self) -> None:
        self.assertEqual(self.amd.display_id, "NASDAQ:AMD")
        self.assertNotEqual(
            self.amd,
            InstrumentKey("NYSE", "AMD", AssetType.EQUITY, Currency.USD),
        )
        self.assertNotEqual(
            self.amd,
            InstrumentKey("NASDAQ", "AMD", AssetType.ETF, Currency.USD),
        )
        self.assertNotEqual(
            self.amd,
            InstrumentKey("NASDAQ", "AMD", AssetType.EQUITY, Currency.KRW),
        )

    def test_decimal_model_refuses_float(self) -> None:
        with self.assertRaises(TypeError):
            Position("kiwoom", "a", self.amd, 0.1)  # type: ignore[arg-type]

    def test_weighted_cost_fx_cash_and_weights(self) -> None:
        positions = (
            BrokerResult.ok(
                "kiwoom",
                [
                    Position(
                        "kiwoom",
                        "k1",
                        self.amd,
                        "10",
                        average_price="100",
                        current_price="150",
                        company_name="AMD",
                    )
                ],
            ),
            BrokerResult.ok(
                "toss",
                [
                    Position(
                        "toss",
                        "t1",
                        self.amd,
                        "5",
                        average_price="120",
                        current_price="150",
                        company_name="Advanced Micro Devices",
                    )
                ],
            ),
        )
        cash = (
            BrokerResult.ok("kiwoom", [CashBalance("kiwoom", "k1", Currency.USD, "100")]),
            BrokerResult.ok("toss", [CashBalance("toss", "t1", Currency.KRW, "100000")]),
        )
        snapshot = PortfolioAggregator(
            Currency.KRW,
            [FxQuote(Currency.USD, Currency.KRW, "1300")],
        ).aggregate(positions, expected_brokers=("kiwoom", "toss"), cash_results=cash)

        self.assertTrue(snapshot.is_complete)
        self.assertTrue(snapshot.includes_cash)
        self.assertEqual(snapshot.total_market_value, Money("3155000", Currency.KRW))
        self.assertEqual(snapshot.included_position_value.amount, Decimal("2925000"))
        self.assertEqual(snapshot.included_cash_value.amount, Decimal("230000"))
        self.assertEqual(snapshot.total_cost_basis, Money("2080000", Currency.KRW))
        position = snapshot.positions[0]
        self.assertEqual(position.quantity, Decimal("15"))
        self.assertEqual(position.cost_basis, Decimal("1600"))
        self.assertEqual(position.average_price, Decimal("1600") / Decimal("15"))
        self.assertEqual(position.market_value, Decimal("2250"))
        self.assertEqual(position.unrealized_profit, Decimal("650"))
        self.assertEqual(position.return_pct, Decimal("40.62500"))
        self.assertEqual(
            position.weight_pct,
            Decimal("2925000") / Decimal("3155000") * Decimal("100"),
        )
        self.assertEqual(
            [(item.broker, item.quantity) for item in position.broker_quantities],
            [("kiwoom", Decimal("10")), ("toss", Decimal("5"))],
        )

    def test_partial_broker_is_subtotal_and_suppresses_weights(self) -> None:
        first = Position("kiwoom", "k1", self.amd, "1", average_price="100", current_price="110")
        second = Position("toss", "t1", self.amd, "2", average_price="90", current_price="110")
        partial = BrokerResult.failure(
            "toss",
            BrokerStatus.PARTIAL,
            error=BrokerError("page_timeout", "a page could not be read", retryable=True),
            items=[second],
            missing_accounts=("masked-account",),
        )
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (BrokerResult.ok("kiwoom", [first]), partial),
            expected_brokers=("kiwoom", "toss"),
        )
        self.assertFalse(snapshot.is_complete)
        self.assertIsNone(snapshot.total_market_value)
        self.assertEqual(snapshot.included_market_value.amount, Decimal("330"))
        self.assertIsNone(snapshot.positions[0].weight_pct)
        self.assertIn("toss", snapshot.incomplete_brokers)
        self.assertIn("toss:positions_partial", snapshot.warnings)

    def test_missing_expected_broker_is_not_zero(self) -> None:
        position = Position("kiwoom", "k1", self.amd, "1", average_price="100", current_price="110")
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (BrokerResult.ok("kiwoom", [position]),),
            expected_brokers=("kiwoom", "meritz"),
        )
        self.assertFalse(snapshot.is_complete)
        self.assertIsNone(snapshot.total_market_value)
        self.assertEqual(snapshot.included_market_value.amount, Decimal("110"))
        self.assertIn("meritz:position_result_missing", snapshot.warnings)

    def test_missing_fx_withholds_total_and_weight(self) -> None:
        position = Position("kiwoom", "k1", self.amd, "1", average_price="100", current_price="110")
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(
            (BrokerResult.ok("kiwoom", [position]),),
            expected_brokers=("kiwoom",),
        )
        self.assertFalse(snapshot.is_complete)
        self.assertIsNone(snapshot.total_market_value)
        self.assertEqual(snapshot.included_market_value.amount, Decimal("0"))
        self.assertIsNone(snapshot.positions[0].market_value_base)
        self.assertIsNone(snapshot.positions[0].weight_pct)

    def test_fx_quote_supports_explicit_inverse_conversion(self) -> None:
        quote = FxQuote(Currency.USD, Currency.KRW, "1300")
        self.assertEqual(
            quote.convert(Money("2600", Currency.KRW), Currency.USD),
            Money("2", Currency.USD),
        )

    def test_partial_cash_result_also_suppresses_total_and_weights(self) -> None:
        position = Position("kiwoom", "k1", self.amd, "1", market_value="100")
        cash = BrokerResult.failure(
            "kiwoom",
            BrokerStatus.PARTIAL,
            error=BrokerError("page_timeout", "cash page incomplete", retryable=True),
            items=(CashBalance("kiwoom", "k1", Currency.USD, "10"),),
            missing_accounts=("masked-account",),
        )
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (BrokerResult.ok("kiwoom", (position,)),),
            expected_brokers=("kiwoom",),
            cash_results=(cash,),
        )
        self.assertFalse(snapshot.is_complete)
        self.assertEqual(snapshot.included_market_value.amount, Decimal("110"))
        self.assertIsNone(snapshot.total_market_value)
        self.assertIsNone(snapshot.positions[0].weight_pct)

    def test_generic_us_market_merges_with_one_known_exchange(self) -> None:
        exact = Position(
            "kiwoom",
            "k1",
            InstrumentKey("NASDAQ", "AMD", currency=Currency.USD),
            "10",
            market_value="1000",
        )
        generic = Position(
            "toss",
            "t1",
            InstrumentKey("US", "AMD", currency=Currency.USD),
            "7",
            market_value="700",
        )
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok("kiwoom", (exact,)),
                BrokerResult.ok("toss", (generic,)),
            ),
            expected_brokers=("kiwoom", "toss"),
        )

        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].instrument.display_id, "NASDAQ:AMD")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("17"))

    def test_generic_us_market_does_not_merge_two_known_exchanges(self) -> None:
        positions = (
            Position(
                "kiwoom",
                "k1",
                InstrumentKey("NASDAQ", "SAME", currency=Currency.USD),
                "1",
                market_value="10",
            ),
            Position(
                "meritz",
                "m1",
                InstrumentKey("NYSE", "SAME", currency=Currency.USD),
                "1",
                market_value="10",
            ),
            Position(
                "toss",
                "t1",
                InstrumentKey("US", "SAME", currency=Currency.USD),
                "1",
                market_value="10",
            ),
        )
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (
                BrokerResult.ok("kiwoom", (positions[0],)),
                BrokerResult.ok("meritz", (positions[1],)),
                BrokerResult.ok("toss", (positions[2],)),
            ),
            expected_brokers=("kiwoom", "meritz", "toss"),
        )

        self.assertEqual(
            {position.instrument.market for position in snapshot.positions},
            {"NASDAQ", "NYSE", "US"},
        )


class RecurringBuyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instrument = InstrumentKey("NASDAQ", "AMD", currency=Currency.USD)
        self.plan = RecurringBuyPlan(
            self.instrument,
            Money("20", Currency.USD),
            broker="toss",
            account_id="account-a",
            target_weight_pct="8",
        )
        self.target_date = date(2026, 8, 10)  # Monday
        self.cutoff = datetime(2026, 8, 10, 18, 0)

    def transaction(self, transaction_id: str, amount: str) -> Transaction:
        return Transaction(
            transaction_id,
            "toss",
            "account-a",
            self.instrument,
            TransactionSide.BUY,
            quantity="1",
            price=amount,
            gross_amount=amount,
            trade_date=self.target_date,
        )

    def test_pending_is_not_called_missing_before_cutoff(self) -> None:
        evaluation = evaluate_recurring_buy(
            self.plan,
            (),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 17, 0),
            cutoff_at=self.cutoff,
        )
        self.assertEqual(evaluation.status, RecurringBuyStatus.PENDING)
        self.assertFalse(evaluation.is_final)

    def test_missing_only_after_cutoff(self) -> None:
        evaluation = evaluate_recurring_buy(
            self.plan,
            (),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 19, 0),
            cutoff_at=self.cutoff,
        )
        self.assertEqual(evaluation.status, RecurringBuyStatus.MISSING)
        self.assertTrue(evaluation.is_final)

    def test_partial_and_tolerance_fill(self) -> None:
        partial = evaluate_recurring_buy(
            self.plan,
            (self.transaction("one", "10"),),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 17, 0),
            cutoff_at=self.cutoff,
        )
        self.assertEqual(partial.status, RecurringBuyStatus.PARTIALLY_FILLED)
        self.assertEqual(partial.remaining.amount, Decimal("10"))
        self.assertFalse(partial.is_final)

        filled = evaluate_recurring_buy(
            self.plan,
            (self.transaction("one", "10"), self.transaction("two", "9.9")),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 17, 0),
            cutoff_at=self.cutoff,
        )
        self.assertEqual(filled.status, RecurringBuyStatus.FILLED)
        self.assertEqual(filled.actual.amount, Decimal("19.9"))

    def test_incomplete_data_is_unverifiable_and_holiday_not_due(self) -> None:
        unavailable = evaluate_recurring_buy(
            self.plan,
            (self.transaction("known-fill", "10"),),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 19, 0),
            cutoff_at=self.cutoff,
            data_complete=False,
        )
        self.assertEqual(unavailable.status, RecurringBuyStatus.UNVERIFIABLE)
        self.assertEqual(unavailable.actual.amount, Decimal("10"))
        self.assertEqual(unavailable.matched_transaction_ids, ("known-fill",))
        holiday = evaluate_recurring_buy(
            self.plan,
            (),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 19, 0),
            cutoff_at=self.cutoff,
            is_trading_day=False,
        )
        self.assertEqual(holiday.status, RecurringBuyStatus.NOT_DUE)

    def test_generic_us_transaction_matches_specific_exchange_plan(self) -> None:
        generic = Transaction(
            "generic-us",
            "toss",
            "account-a",
            InstrumentKey("US", "AMD", currency=Currency.USD),
            TransactionSide.BUY,
            quantity="0.1",
            price="200",
            gross_amount="20",
            trade_date=self.target_date,
        )
        evaluation = evaluate_recurring_buy(
            self.plan,
            (generic,),
            target_date=self.target_date,
            now=datetime(2026, 8, 10, 19, 0),
            cutoff_at=self.cutoff,
        )

        self.assertEqual(evaluation.status, RecurringBuyStatus.FILLED)


if __name__ == "__main__":
    unittest.main()
