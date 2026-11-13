"""Toss Securities official REST Open API read-only adapter."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from account_bot.domain import (
    Account,
    AssetType,
    BrokerResult,
    CashBalance,
    Currency,
    InstrumentKey,
    Position,
    Transaction,
    TransactionSide,
)

from .base import ReadOnlyBroker
from .helpers import (
    decimal_string,
    failure_result,
    not_configured,
    now_utc,
    required_list,
    required_mapping,
)
from .http import ReadOnlyHttpClient, RequestRule

_KST = ZoneInfo("Asia/Seoul")
_GET_PATHS = {
    "/api/v1/accounts",
    "/api/v1/holdings",
    "/api/v1/orders",
    "/api/v1/buying-power",
}


class TossBroker(ReadOnlyBroker):
    name = "toss"

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        base_url: str | None = None,
        http_client: ReadOnlyHttpClient | None = None,
    ) -> None:
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        if http_client is not None:
            self._http = http_client
        else:
            origin = (base_url or "https://openapi.tossinvest.com").rstrip("/")
            if origin != "https://openapi.tossinvest.com":
                raise ValueError("Toss base URL must be the approved official origin")
            self._http = ReadOnlyHttpClient(
                origin,
                {RequestRule("POST", "/oauth2/token")}
                | {RequestRule("GET", path) for path in _GET_PATHS},
            )
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._token_lock = asyncio.Lock()
        self._accounts_cache: tuple[Account, ...] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def _access_token(self) -> str:
        if self._token and self._token_expires_at > now_utc() + timedelta(minutes=5):
            return self._token
        async with self._token_lock:
            if self._token and self._token_expires_at > now_utc() + timedelta(minutes=5):
                return self._token
            payload, _ = await self._http.request_json(
                "POST",
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("Toss token response was incomplete")
            expires = int(payload.get("expires_in") or 86400)
            self._token = token
            self._token_expires_at = now_utc() + timedelta(seconds=max(60, expires - 300))
            return token

    async def _get(
        self,
        path: str,
        *,
        account_id: str | None = None,
        params: dict[str, str] | None = None,
    ) -> dict:
        if path not in _GET_PATHS:
            raise ValueError("path is not a read-only Toss endpoint")
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_id is not None:
            headers["X-Tossinvest-Account"] = account_id
        payload, _ = await self._http.request_json("GET", path, headers=headers, params=params)
        return payload

    async def get_accounts(self) -> BrokerResult[Account]:
        if not self.configured:
            return not_configured(
                self.name, "토스증권 Open API 사전 신청 및 인증정보가 필요합니다."
            )
        try:
            payload = await self._get("/api/v1/accounts")
            accounts = tuple(
                Account(
                    self.name,
                    str(row["accountSeq"]),
                    f"토스 {row.get('accountType', '계좌')}",
                    Currency.KRW,
                )
                for row in required_list(payload, "result")
            )
            if not accounts:
                raise RuntimeError("Toss account response contained no linked account")
            self._accounts_cache = accounts
            return BrokerResult.ok(self.name, accounts, as_of=now_utc())
        except Exception as exc:
            return failure_result(self.name, exc)

    async def _accounts(self) -> tuple[Account, ...]:
        if self._accounts_cache is not None:
            return self._accounts_cache
        result = await self.get_accounts()
        if result.status.value != "ok":
            raise RuntimeError("Toss account lookup failed")
        return result.items

    @staticmethod
    def _instrument(row: dict) -> InstrumentKey:
        currency = Currency(str(row.get("currency") or "").upper())
        country = str(row.get("marketCountry") or "").upper()
        if country in {"KR", "KOR"}:
            market = "KRX"
        elif country in {"US", "USA"}:
            market = "US"
        else:
            raise ValueError("Toss holding omitted a supported market country")
        ticker = str(row.get("symbol") or "").strip().upper()
        return InstrumentKey(market, ticker, AssetType.EQUITY, currency)

    async def get_positions(self) -> BrokerResult[Position]:
        if not self.configured:
            return not_configured(
                self.name, "토스증권 Open API 사전 신청 및 인증정보가 필요합니다."
            )
        try:
            as_of = now_utc()
            positions: list[Position] = []
            for account in await self._accounts():
                payload = await self._get("/api/v1/holdings", account_id=account.account_id)
                overview = required_mapping(payload, "result")
                for row in required_list(overview, "items"):
                    market_value = required_mapping(row, "marketValue")
                    profit_loss = required_mapping(row, "profitLoss")
                    positions.append(
                        Position(
                            broker=self.name,
                            account_id=account.account_id,
                            instrument=self._instrument(row),
                            quantity=decimal_string(row.get("quantity")),
                            average_price=decimal_string(
                                row.get("averagePurchasePrice"), absolute=True
                            ),
                            current_price=decimal_string(row.get("lastPrice"), absolute=True),
                            cost_basis=decimal_string(
                                market_value.get("purchaseAmount"), absolute=True
                            ),
                            market_value=decimal_string(market_value.get("amount"), absolute=True),
                            unrealized_profit=decimal_string(profit_loss.get("amount")),
                            return_pct=decimal_string(profit_loss.get("rate")) * Decimal("100"),
                            company_name=str(row.get("name") or "").strip(),
                            as_of=as_of,
                        )
                    )
            return BrokerResult.ok(self.name, positions, as_of=as_of)
        except Exception as exc:
            return failure_result(self.name, exc)

    async def get_cash(self) -> BrokerResult[CashBalance]:
        if not self.configured:
            return not_configured(
                self.name, "토스증권 Open API 사전 신청 및 인증정보가 필요합니다."
            )
        try:
            as_of = now_utc()
            balances: list[CashBalance] = []
            for account in await self._accounts():
                for currency in (Currency.KRW, Currency.USD):
                    payload = await self._get(
                        "/api/v1/buying-power",
                        account_id=account.account_id,
                        params={"currency": currency.value},
                    )
                    result = required_mapping(payload, "result")
                    balances.append(
                        CashBalance(
                            self.name,
                            account.account_id,
                            currency,
                            decimal_string(result.get("cashBuyingPower")),
                            as_of,
                        )
                    )
            return BrokerResult.ok(self.name, balances, as_of=as_of)
        except Exception as exc:
            return failure_result(self.name, exc)

    async def get_transactions(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> BrokerResult[Transaction]:
        if not self.configured:
            return not_configured(
                self.name, "토스증권 Open API 사전 신청 및 인증정보가 필요합니다."
            )
        if start_date > end_date:
            return failure_result(self.name, ValueError("invalid date range"))
        try:
            transactions: list[Transaction] = []
            for account in await self._accounts():
                cursor: str | None = None
                for _ in range(100):
                    params = {
                        "status": "CLOSED",
                        # A KST calendar day begins on the preceding UTC date.
                        "from": (start_date - timedelta(days=1)).isoformat(),
                        "to": end_date.isoformat(),
                        "limit": "100",
                    }
                    if cursor:
                        params["cursor"] = cursor
                    payload = await self._get(
                        "/api/v1/orders", account_id=account.account_id, params=params
                    )
                    result = required_mapping(payload, "result")
                    for row in required_list(result, "orders"):
                        execution = required_mapping(row, "execution")
                        quantity = decimal_string(execution.get("filledQuantity"), absolute=True)
                        if quantity <= 0:
                            continue
                        filled_at_text = execution.get("filledAt") or row.get("orderedAt")
                        try:
                            executed_at = datetime.fromisoformat(
                                str(filled_at_text).replace("Z", "+00:00")
                            )
                        except ValueError:
                            executed_at = datetime.combine(
                                start_date, datetime.min.time(), tzinfo=timezone.utc
                            )
                        if executed_at.tzinfo is None:
                            executed_at = executed_at.replace(tzinfo=timezone.utc)
                        trade_date = executed_at.astimezone(_KST).date()
                        if not (start_date <= trade_date <= end_date):
                            continue
                        currency = Currency(str(row.get("currency") or "KRW").upper())
                        ticker = str(row.get("symbol") or "").strip().upper()
                        market = "KRX" if currency == Currency.KRW else "US"
                        transactions.append(
                            Transaction(
                                transaction_id=str(row.get("orderId") or "unknown"),
                                broker=self.name,
                                account_id=account.account_id,
                                instrument=InstrumentKey(
                                    market, ticker, AssetType.EQUITY, currency
                                ),
                                side=TransactionSide(str(row.get("side") or "BUY").upper()),
                                quantity=quantity,
                                price=decimal_string(
                                    execution.get("averageFilledPrice"), absolute=True
                                ),
                                gross_amount=decimal_string(
                                    execution.get("filledAmount"), absolute=True
                                ),
                                fee=decimal_string(
                                    execution.get("commission"),
                                    absolute=True,
                                    default_zero=True,
                                ),
                                tax=decimal_string(
                                    execution.get("tax"),
                                    absolute=True,
                                    default_zero=True,
                                ),
                                trade_date=trade_date,
                                executed_at=executed_at,
                            )
                        )
                    if not result.get("hasNext"):
                        break
                    cursor = str(result.get("nextCursor") or "")
                    if not cursor:
                        raise RuntimeError("Toss pagination cursor missing")
                else:
                    raise RuntimeError("Toss pagination limit exceeded")
            return BrokerResult.ok(self.name, transactions, as_of=now_utc())
        except Exception as exc:
            return failure_result(self.name, exc)
