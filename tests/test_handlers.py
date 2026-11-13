from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.config import Settings  # noqa: E402
from account_bot.domain import AssetType, Currency, InstrumentKey, Money  # noqa: E402
from account_bot.portfolio import RecurringBuyPlan  # noqa: E402
from account_bot.telegram_ui.auth import authorized_handler  # noqa: E402
from account_bot.telegram_ui.handlers import TelegramCommandController  # noqa: E402


def make_settings() -> Settings:
    return Settings.load(
        environ={
            "ACCOUNT_BOT_TELEGRAM_TOKEN": "fixture-telegram-token",
            "ALLOWED_TELEGRAM_USER_IDS": "1001",
        },
        base_dir=PROJECT_ROOT,
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append((text, kwargs))


class FakeUpdate:
    def __init__(self, user_id: int, text: str) -> None:
        self.effective_user = FakeUser(user_id)
        self.effective_chat = SimpleNamespace(id=user_id, type="private")
        self.effective_message = FakeMessage(text)


class FakeRecurringGateway:
    def __init__(self, plans: tuple[RecurringBuyPlan, ...] = ()) -> None:
        self.upserts: list[dict] = []
        self.plans = plans
        self.theses: list[dict] = []
        self.group_changes: list[dict] = []

    async def list_plans(self, *, enabled_only: bool = False):
        return self.plans

    async def upsert_plan(self, **kwargs) -> RecurringBuyPlan:
        self.upserts.append(kwargs)
        instrument = InstrumentKey(
            kwargs["market"],
            kwargs["ticker"],
            currency=kwargs["currency"],
        )
        return RecurringBuyPlan(
            instrument=instrument,
            daily_amount=Money(kwargs["amount"], kwargs["currency"]),
            target_weight_pct=kwargs["target_weight_pct"],
            memo=kwargs["memo"],
        )

    async def set_thesis(self, **kwargs) -> None:
        self.theses.append(kwargs)

    async def apply_group_change(self, **kwargs):
        self.group_changes.append(kwargs)
        return (
            RecurringBuyPlan(
                InstrumentKey("US", "QQQ", AssetType.ETF, Currency.USD),
                Money("5000", Currency.KRW),
                broker="kiwoom_accum",
            ),
        )


class RecurringAddHandlerTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, gateway: FakeRecurringGateway) -> TelegramCommandController:
        return TelegramCommandController(
            settings=make_settings(),
            portfolios=object(),  # type: ignore[arg-type]
            recurring=gateway,
        )

    async def test_recurring_add_parses_amount_currency_weight_and_memo(self) -> None:
        gateway = FakeRecurringGateway()
        controller = self.controller(gateway)
        update = FakeUpdate(
            1001,
            "/recurring_add AMD 20USD 8% data center growth",
        )

        await controller.recurring_add(update, object())

        self.assertEqual(
            gateway.upserts,
            [
                {
                    "market": "NASDAQ",
                    "ticker": "AMD",
                    "amount": Decimal("20"),
                    "currency": Currency.USD,
                    "target_weight_pct": Decimal("8"),
                    "memo": "data center growth",
                }
            ],
        )
        self.assertEqual(len(update.effective_message.replies), 1)
        reply, options = update.effective_message.replies[0]
        self.assertIn("NASDAQ:AMD", reply)
        self.assertIn("주문은 실행되지 않습니다", reply)
        self.assertEqual(options["parse_mode"], "HTML")

    async def test_unauthorized_recurring_add_is_blocked_by_auth_wrapper(self) -> None:
        gateway = FakeRecurringGateway()
        controller = self.controller(gateway)
        update = FakeUpdate(9999, "/recurring_add AMD 20USD 8%")
        guarded = authorized_handler(
            controller.settings.allowed_telegram_user_ids,
            controller.recurring_add,
        )

        result = await guarded(update, object())

        self.assertIsNone(result)
        self.assertEqual(gateway.upserts, [])
        self.assertEqual(
            update.effective_message.replies,
            [("이 계좌관리봇을 사용할 권한이 없습니다.", {})],
        )

    async def test_recurring_update_parses_group_exclusions_and_delta(self) -> None:
        gateway = FakeRecurringGateway()
        controller = self.controller(gateway)
        update = FakeUpdate(1001, "/모으기변경 개별주 NVDA, AMD 빼고 -1000")

        await controller.recurring_update(update, object())

        self.assertEqual(
            gateway.group_changes,
            [{
                "asset_type": AssetType.EQUITY,
                "delta": Decimal("-1000"),
                "stop": False,
                "excluded": frozenset({"NVDA", "AMD"}),
            }],
        )

    async def test_recurring_update_parses_etf_stop(self) -> None:
        gateway = FakeRecurringGateway()
        controller = self.controller(gateway)
        update = FakeUpdate(1001, "/모으기변경 ETF 전부 모으기중단")

        await controller.recurring_update(update, object())

        self.assertTrue(gateway.group_changes[0]["stop"])
        self.assertEqual(gateway.group_changes[0]["asset_type"], AssetType.ETF)

    async def test_thesis_uses_unique_compatible_recurring_listing(self) -> None:
        plan = RecurringBuyPlan(
            InstrumentKey("NASDAQ", "AMD", currency=Currency.USD),
            Money("20", Currency.USD),
            target_weight_pct="8",
        )
        gateway = FakeRecurringGateway((plan,))
        controller = self.controller(gateway)
        update = FakeUpdate(1001, "/thesis US:AMD data center growth")

        await controller.thesis(update, object())

        self.assertEqual(
            gateway.theses,
            [
                {
                    "market": "NASDAQ",
                    "ticker": "AMD",
                    "thesis": "data center growth",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
