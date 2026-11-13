from __future__ import annotations

import unittest
from unittest.mock import patch

from account_bot.market.yfinance_provider import YFinanceProvider, yahoo_fx_candidates


class YahooFinanceProviderTests(unittest.TestCase):
    def test_usd_krw_uses_yahoo_live_symbol_and_inverse_is_explicit(self) -> None:
        self.assertEqual(yahoo_fx_candidates("USD", "KRW"), (("KRW=X", False),))
        self.assertEqual(yahoo_fx_candidates("KRW", "USD"), (("KRW=X", True),))


class YahooFinanceFxTests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_endpoint_is_used_before_yfinance_wrapper(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "regularMarketPrice": 1416.88,
                                    "regularMarketTime": 1786517106,
                                }
                            }
                        ]
                    }
                }

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, params):
                self.url = url
                self.params = params
                return FakeResponse()

        with patch(
            "account_bot.market.yfinance_provider.httpx.AsyncClient",
            return_value=FakeClient(),
        ):
            quote = await YFinanceProvider().get_fx("USD", "KRW")

        self.assertEqual(str(quote.rate), "1416.88")
        self.assertEqual(quote.provider, "yahoo-chart")
        self.assertFalse(quote.stale)


if __name__ == "__main__":
    unittest.main()
