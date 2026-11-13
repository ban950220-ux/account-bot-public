from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.brokers import ReadOnlyBroker  # noqa: E402
from account_bot.domain import (  # noqa: E402
    Account,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    InstrumentKey,
    Position,
    Transaction,
)
from account_bot.services import AccountBotService  # noqa: E402


class FreshFxProvider:
    async def get_fx(self, base: str, quote: str):
        from account_bot.market import FxQuote as MarketFxQuote

        return MarketFxQuote(
            base=base,
            quote=quote,
            rate=Decimal("1400"),
            as_of=datetime.now(timezone.utc),
            provider="fixture",
        )


class FailingFxStore:
    def __init__(self) -> None:
        self.fallback_reads = 0

    async def save_fx_rate(self, **_kwargs) -> None:
        raise RuntimeError("fixture persistence failure")

    async def get_last_fx_rate(self, **_kwargs):
        self.fallback_reads += 1
        return None


class OneShotFxProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.observed_at = datetime.now(timezone.utc)

    async def get_fx(self, base: str, quote: str):
        self.calls += 1
        if self.calls > 1:
            raise TimeoutError("fixture provider outage")
        from account_bot.market import FxQuote as MarketFxQuote

        return MarketFxQuote(
            base=base,
            quote=quote,
            rate=Decimal("1400"),
            as_of=self.observed_at,
            provider="fixture",
        )


class OlderFxStore(FailingFxStore):
    async def get_last_fx_rate(self, **_kwargs):
        self.fallback_reads += 1
        return Decimal("1300"), datetime.now(timezone.utc) - timedelta(days=1), "old-store"


class HealthyBroker(ReadOnlyBroker):
    name = "kiwoom"

    @property
    def configured(self) -> bool:
        return True

    async def get_accounts(self) -> BrokerResult[Account]:
        return BrokerResult.ok(
            self.name,
            (Account(self.name, "safe-account", "테스트 계좌", Currency.KRW),),
        )

    async def get_positions(self) -> BrokerResult[Position]:
        instrument = InstrumentKey("KRX", "005930", currency=Currency.KRW)
        return BrokerResult.ok(
            self.name,
            (
                Position(
                    self.name,
                    "safe-account",
                    instrument,
                    quantity="10",
                    cost_basis="1000000",
                    market_value="1200000",
                ),
            ),
        )

    async def get_cash(self) -> BrokerResult[CashBalance]:
        return BrokerResult.ok(
            self.name,
            (CashBalance(self.name, "safe-account", Currency.KRW, "300000"),),
        )

    async def get_transactions(
        self, *, start_date: date, end_date: date
    ) -> BrokerResult[Transaction]:
        return BrokerResult.ok(self.name)


class FailingBroker(ReadOnlyBroker):
    name = "toss"

    @property
    def configured(self) -> bool:
        return True

    async def get_accounts(self) -> BrokerResult[Account]:
        return BrokerResult.ok(
            self.name,
            (Account(self.name, "safe-account", "테스트 계좌", Currency.KRW),),
        )

    async def get_positions(self) -> BrokerResult[Position]:
        raise TimeoutError("fixture timeout without provider payload")

    async def get_cash(self) -> BrokerResult[CashBalance]:
        raise TimeoutError("fixture timeout without provider payload")

    async def get_transactions(
        self, *, start_date: date, end_date: date
    ) -> BrokerResult[Transaction]:
        raise TimeoutError("fixture timeout without provider payload")


class UsdBroker(HealthyBroker):
    async def get_positions(self) -> BrokerResult[Position]:
        instrument = InstrumentKey("NASDAQ", "AMD", currency=Currency.USD)
        return BrokerResult.ok(
            self.name,
            (
                Position(
                    self.name,
                    "safe-account",
                    instrument,
                    quantity="1",
                    cost_basis="90",
                    market_value="100",
                ),
            ),
        )

    async def get_cash(self) -> BrokerResult[CashBalance]:
        return BrokerResult.ok(self.name)


class PortfolioServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_can_scope_the_complete_denominator_to_one_broker(self) -> None:
        service = AccountBotService(
            (HealthyBroker(), FailingBroker()),
            market_provider=None,
            broker_timeout_seconds=1,
        )

        refresh = await service.refresh_portfolio(broker_names={"kiwoom"})

        self.assertEqual(refresh.snapshot.expected_brokers, ("kiwoom",))
        self.assertTrue(refresh.snapshot.is_complete)
        self.assertEqual({result.broker for result in refresh.position_results}, {"kiwoom"})
        self.assertEqual(refresh.snapshot.total_market_value.amount, Decimal("1500000"))

    async def test_failed_broker_is_not_substituted_with_zero(self) -> None:
        service = AccountBotService(
            (HealthyBroker(), FailingBroker()),
            market_provider=None,
            broker_timeout_seconds=1,
        )

        refresh = await service.refresh_portfolio()

        statuses = {result.broker: result.status for result in refresh.position_results}
        cash_statuses = {result.broker: result.status for result in refresh.cash_results}
        self.assertEqual(statuses["kiwoom"], BrokerStatus.OK)
        self.assertEqual(statuses["toss"], BrokerStatus.UNAVAILABLE)
        self.assertEqual(cash_statuses["kiwoom"], BrokerStatus.OK)
        self.assertEqual(cash_statuses["toss"], BrokerStatus.UNAVAILABLE)

        snapshot = refresh.snapshot
        self.assertFalse(snapshot.is_complete)
        self.assertEqual(snapshot.incomplete_brokers, ("toss",))
        self.assertEqual(snapshot.included_brokers, ("kiwoom",))
        self.assertEqual(snapshot.included_position_value.amount, Decimal("1200000"))
        self.assertEqual(snapshot.included_cash_value.amount, Decimal("300000"))
        self.assertEqual(snapshot.included_market_value.amount, Decimal("1500000"))
        # The known amount remains a labelled subtotal. A false all-broker total
        # and portfolio denominator are deliberately unavailable.
        self.assertIsNone(snapshot.total_market_value)
        self.assertFalse(snapshot.weights_available)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertIsNone(snapshot.positions[0].weight_pct)
        self.assertIn("toss:positions_unavailable", snapshot.warnings)
        self.assertIn("toss:cash_unavailable", snapshot.warnings)

    async def test_fresh_fx_survives_best_effort_store_failure(self) -> None:
        store = FailingFxStore()
        service = AccountBotService(
            (UsdBroker(),),
            market_provider=FreshFxProvider(),
            fx_store=store,
            broker_timeout_seconds=1,
        )

        refresh = await service.refresh_portfolio()

        self.assertIsNotNone(refresh.fx_quote)
        self.assertFalse(refresh.fx_quote.is_stale)
        self.assertEqual(refresh.snapshot.total_market_value.amount, Decimal("140000"))
        self.assertEqual(store.fallback_reads, 0)

    async def test_newer_in_memory_fx_wins_when_store_save_then_provider_fail(self) -> None:
        provider = OneShotFxProvider()
        store = OlderFxStore()
        service = AccountBotService(
            (UsdBroker(),),
            market_provider=provider,
            fx_store=store,
            broker_timeout_seconds=1,
        )

        first = await service.refresh_portfolio()
        second = await service.refresh_portfolio()

        self.assertEqual(first.fx_quote.rate, Decimal("1400"))
        self.assertFalse(first.fx_quote.is_stale)
        self.assertEqual(second.fx_quote.rate, Decimal("1400"))
        self.assertTrue(second.fx_quote.is_stale)
        self.assertEqual(second.snapshot.total_market_value.amount, Decimal("140000"))
        self.assertEqual(store.fallback_reads, 1)


if __name__ == "__main__":
    unittest.main()
