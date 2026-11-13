from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from account_bot.services.storage_adapters import SnapshotAugmentedRecurringPlanGateway


class _EmptyGateway:
    async def list_plans(self, *, enabled_only: bool = False):
        del enabled_only
        return ()


class RecurringSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_krw_funded_us_plan_is_loaded_without_account_identifier(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recurring.json").write_text(
                json.dumps(
                    {
                        "as_of": "2026-08-11T00:00:00Z",
                        "plans": [
                            {
                                "market": "US",
                                "ticker": "DGRO",
                                "asset_type": "ETF",
                                "instrument_currency": "USD",
                                "daily_amount": "3000",
                                "amount_currency": "KRW",
                                "broker": "meritz",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            gateway = SnapshotAugmentedRecurringPlanGateway(_EmptyGateway(), root)
            plans = await gateway.list_plans()

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].broker, "meritz")
        self.assertEqual(plans[0].instrument.currency.value, "USD")
        self.assertEqual(plans[0].daily_amount.currency.value, "KRW")
        self.assertIsNone(plans[0].account_id)


if __name__ == "__main__":
    unittest.main()
