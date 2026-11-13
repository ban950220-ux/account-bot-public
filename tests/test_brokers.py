from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.brokers.helpers import failure_result  # noqa: E402
from account_bot.brokers.http import (  # noqa: E402
    ReadOnlyHttpClient,
    ReadOnlyPolicyError,
    RequestRule,
)
from account_bot.brokers.kiwoom import KiwoomBroker  # noqa: E402
from account_bot.brokers.snapshot import JsonSnapshotBroker  # noqa: E402
from account_bot.brokers.toss import TossBroker  # noqa: E402
from account_bot.domain import (  # noqa: E402
    AssetType,
    BrokerStatus,
    Currency,
)


class KiwoomHttpStub:
    """Returns frozen provider fixtures and never opens a socket."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers=None,
        params=None,
        json=None,
        data=None,
    ):
        api_id = (headers or {}).get("api-id", "")
        self.calls.append((method.upper(), path, api_id))
        if method.upper() == "POST" and path == "/oauth2/token":
            return {"return_code": 0, "token": "fixture-token"}, {}
        if method.upper() == "POST" and path == "/api/dostk/acnt" and api_id == "kt00018":
            return (
                {
                    "return_code": 0,
                    "acnt_evlt_remn_indv_tot": [
                        {
                            "stk_cd": "A005930",
                            "stk_nm": "삼성전자",
                            "rmnd_qty": "10",
                            "pur_pric": "-70,000",
                            "cur_prc": "-75,500",
                            "pur_amt": "-700,000",
                            "evlt_amt": "755,000",
                            "evltv_prft": "55,000",
                            "prft_rt": "7.8571",
                        }
                    ],
                },
                {"cont-yn": "N"},
            )
        raise AssertionError(f"unexpected fake Kiwoom request: {method} {path} {api_id}")


class TossHttpStub:
    """Small scripted Toss API surface used by the normalization test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers=None,
        params=None,
        json=None,
        data=None,
    ):
        account = (headers or {}).get("X-Tossinvest-Account")
        self.calls.append((method.upper(), path, account))
        if method.upper() == "POST" and path == "/oauth2/token":
            return {"access_token": "fixture-token", "expires_in": 3600}, {}
        if method.upper() == "GET" and path == "/api/v1/accounts":
            return {"result": [{"accountSeq": "safe-sequence", "accountType": "위탁"}]}, {}
        if method.upper() == "GET" and path == "/api/v1/holdings":
            if account != "safe-sequence":
                raise AssertionError("holdings request did not use the opaque account sequence")
            return {
                "result": {
                    "items": [
                        {
                            "marketCountry": "US",
                            "currency": "USD",
                            "symbol": "amd",
                            "name": "Advanced Micro Devices",
                            "quantity": "0.119",
                            "averagePurchasePrice": "142.50",
                            "lastPrice": "168.20",
                            "marketValue": {
                                "purchaseAmount": "16.9575",
                                "amount": "20.0158",
                            },
                            "profitLoss": {"amount": "3.0583", "rate": "0.18035"},
                        }
                    ]
                }
            }, {}
        raise AssertionError(f"unexpected fake Toss request: {method} {path}")


class MissingKiwoomRowsStub(KiwoomHttpStub):
    async def request_json(self, method: str, path: str, **kwargs):
        headers = kwargs.get("headers") or {}
        if path == "/api/dostk/acnt" and headers.get("api-id") == "kt00018":
            return {"return_code": 0}, {"cont-yn": "N"}
        return await super().request_json(method, path, **kwargs)


class MissingTossRowsStub(TossHttpStub):
    async def request_json(self, method: str, path: str, **kwargs):
        if path == "/api/v1/holdings":
            return {}, {}
        return await super().request_json(method, path, **kwargs)


class NoNetworkClient:
    """Fails loudly if a rejected policy call reaches the transport layer."""

    def __init__(self) -> None:
        self.request_count = 0

    async def request(self, *args, **kwargs):
        self.request_count += 1
        raise AssertionError("network transport must not be called")


class BrokerNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_snapshot_sections_are_failure_not_zero_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete.json"
            path.write_text("{}", encoding="utf-8")
            broker = JsonSnapshotBroker("meritz", path)

            positions = await broker.get_positions()
            cash = await broker.get_cash()

        self.assertEqual(positions.status, BrokerStatus.UNAVAILABLE)
        self.assertEqual(cash.status, BrokerStatus.UNAVAILABLE)
        self.assertEqual(positions.items, ())
        self.assertEqual(cash.items, ())

    async def test_old_snapshot_row_is_stale_not_current_complete_data(self) -> None:
        payload = """{
          "positions": [{
            "account_id": "meritz-primary",
            "market": "NASDAQ",
            "ticker": "AMD",
            "currency": "USD",
            "quantity": "1",
            "market_value": "100",
            "as_of": "2020-01-01T00:00:00Z"
          }]
        }"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.json"
            path.write_text(payload, encoding="utf-8")
            result = await JsonSnapshotBroker("meritz", path).get_positions()

        self.assertEqual(result.status, BrokerStatus.STALE)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.as_of, datetime(2020, 1, 1, tzinfo=timezone.utc))

    async def test_missing_live_response_lists_are_failure_not_empty_success(self) -> None:
        kiwoom = KiwoomBroker(
            app_key="configured-key",
            app_secret="configured-secret",
            mock=True,
            http_client=MissingKiwoomRowsStub(),  # type: ignore[arg-type]
        )
        toss = TossBroker(
            client_id="configured-client",
            client_secret="configured-secret",
            http_client=MissingTossRowsStub(),  # type: ignore[arg-type]
        )

        kiwoom_result = await kiwoom.get_positions()
        toss_result = await toss.get_positions()

        self.assertEqual(kiwoom_result.status, BrokerStatus.UNAVAILABLE)
        self.assertEqual(toss_result.status, BrokerStatus.UNAVAILABLE)

    async def test_kiwoom_position_is_normalized_to_standard_decimal_model(self) -> None:
        http = KiwoomHttpStub()
        broker = KiwoomBroker(
            app_key="configured-key",
            app_secret="configured-secret",
            mock=True,
            http_client=http,  # type: ignore[arg-type]
        )

        result = await broker.get_positions()

        self.assertEqual(result.status, BrokerStatus.OK)
        self.assertEqual(len(result.items), 1)
        position = result.items[0]
        self.assertEqual(position.broker, "kiwoom")
        self.assertEqual(position.account_id, "kiwoom-primary")
        self.assertEqual(position.instrument.market, "KRX")
        self.assertEqual(position.instrument.ticker, "005930")
        self.assertEqual(position.instrument.asset_type, AssetType.EQUITY)
        self.assertEqual(position.instrument.currency, Currency.KRW)
        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.average_price, Decimal("70000"))
        self.assertEqual(position.current_price, Decimal("75500"))
        self.assertEqual(position.cost_basis, Decimal("700000"))
        self.assertEqual(position.market_value, Decimal("755000"))
        self.assertEqual(position.unrealized_profit, Decimal("55000"))
        self.assertEqual(position.return_pct, Decimal("7.8571"))
        self.assertEqual(
            http.calls,
            [
                ("POST", "/oauth2/token", ""),
                ("POST", "/api/dostk/acnt", "kt00018"),
            ],
        )

    async def test_toss_position_is_normalized_to_standard_decimal_model(self) -> None:
        http = TossHttpStub()
        broker = TossBroker(
            client_id="configured-client",
            client_secret="configured-secret",
            http_client=http,  # type: ignore[arg-type]
        )

        result = await broker.get_positions()

        self.assertEqual(result.status, BrokerStatus.OK)
        self.assertEqual(len(result.items), 1)
        position = result.items[0]
        self.assertEqual(position.broker, "toss")
        self.assertEqual(position.account_id, "safe-sequence")
        self.assertEqual(position.instrument.market, "US")
        self.assertEqual(position.instrument.ticker, "AMD")
        self.assertEqual(position.instrument.asset_type, AssetType.EQUITY)
        self.assertEqual(position.instrument.currency, Currency.USD)
        self.assertEqual(position.quantity, Decimal("0.119"))
        self.assertEqual(position.average_price, Decimal("142.50"))
        self.assertEqual(position.current_price, Decimal("168.20"))
        self.assertEqual(position.cost_basis, Decimal("16.9575"))
        self.assertEqual(position.market_value, Decimal("20.0158"))
        self.assertEqual(position.unrealized_profit, Decimal("3.0583"))
        self.assertEqual(position.return_pct, Decimal("18.03500"))
        self.assertEqual(
            http.calls,
            [
                ("POST", "/oauth2/token", None),
                ("GET", "/api/v1/accounts", None),
                ("GET", "/api/v1/holdings", "safe-sequence"),
            ],
        )


class ReadOnlyPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.transport = NoNetworkClient()
        self.client = ReadOnlyHttpClient(
            "https://broker.invalid",
            {
                RequestRule(
                    "POST",
                    "/api/dostk/acnt",
                    frozenset({"kt00018", "kt00001"}),
                )
            },
            client=self.transport,  # type: ignore[arg-type]
        )

    async def test_broker_credentials_cannot_target_arbitrary_https_origin(self) -> None:
        with self.assertRaises(ValueError):
            KiwoomBroker(
                app_key="configured-key",
                app_secret="configured-secret",
                base_url="https://collector.example",
            )
        with self.assertRaises(ValueError):
            TossBroker(
                client_id="configured-client",
                client_secret="configured-secret",
                base_url="https://collector.example",
            )

    async def test_order_path_is_rejected_before_transport(self) -> None:
        with self.assertRaises(ReadOnlyPolicyError):
            await self.client.request_json(
                "POST",
                "/api/dostk/ordr",
                headers={"api-id": "kt10000"},
                json={"ticker": "005930"},
            )
        self.assertEqual(self.transport.request_count, 0)

    async def test_unallowlisted_tr_is_rejected_before_transport(self) -> None:
        with self.assertRaises(ReadOnlyPolicyError):
            await self.client.request_json(
                "POST",
                "/api/dostk/acnt",
                headers={"api-id": "kt99999"},
                json={},
            )
        self.assertEqual(self.transport.request_count, 0)


class BrokerFailureSanitizationTests(unittest.TestCase):
    def test_http_transport_failure_is_retryable_and_contains_no_provider_payload(self) -> None:
        request = httpx.Request("GET", "https://broker.invalid/read-only")
        result = failure_result(
            "kiwoom",
            httpx.ConnectError("provider-secret-body", request=request),
        )

        self.assertEqual(result.status, BrokerStatus.UNAVAILABLE)
        self.assertEqual(result.errors[0].code, "TRANSPORT_ERROR")
        self.assertTrue(result.errors[0].retryable)
        self.assertNotIn("provider-secret-body", result.errors[0].message)

    def test_adapters_reject_non_official_credential_destinations(self) -> None:
        with self.assertRaises(ValueError):
            KiwoomBroker(
                app_key="configured-key",
                app_secret="configured-secret",
                base_url="https://collector.example",
            )
        with self.assertRaises(ValueError):
            TossBroker(
                client_id="configured-client",
                client_secret="configured-secret",
                base_url="https://collector.example",
            )


if __name__ == "__main__":
    unittest.main()
