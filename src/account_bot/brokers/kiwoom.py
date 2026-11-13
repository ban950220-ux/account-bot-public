"""Kiwoom Securities official REST API read-only adapter.

Only token issuance and the documented account-query TRs are allowlisted.
`/api/dostk/ordr` is not present anywhere in this adapter.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from account_bot.domain import (
    Account,
    AssetType,
    BrokerError,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    InstrumentKey,
    Position,
    Transaction,
    TransactionSide,
)

from .base import ReadOnlyBroker
from .helpers import decimal_string, failure_result, not_configured, now_utc, required_list
from .http import ReadOnlyHttpClient, RequestRule

_KST = ZoneInfo("Asia/Seoul")
_ACCOUNT_PATH = "/api/dostk/acnt"
_READ_ONLY_TRS = frozenset({"kt00018", "kt00001", "ka10076"})
_US_ACCOUNT_PATH = "/api/us/acnt"
_US_READ_ONLY_TRS = frozenset({"ust21070", "ust21110", "ust21510"})


class KiwoomBroker(ReadOnlyBroker):

    def __init__(
        self,
        *,
        app_key: str | None,
        app_secret: str | None,
        broker_name: str = "kiwoom",
        account_id: str = "kiwoom-primary",
        account_alias: str = "키움 계좌",
        mock: bool = False,
        base_url: str | None = None,
        http_client: ReadOnlyHttpClient | None = None,
    ) -> None:
        self.name = broker_name.strip().lower()
        if not self.name:
            raise ValueError("Kiwoom broker name must not be empty")
        self._local_account_id = account_id.strip()
        if not self._local_account_id:
            raise ValueError("Kiwoom local account ID must not be empty")
        self._app_key = (app_key or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._account_alias = account_alias.strip() or "키움 계좌"
        self._mock = mock
        if http_client is not None:
            self._http = http_client
        else:
            origin = (
                base_url or ("https://mockapi.kiwoom.com" if mock else "https://api.kiwoom.com")
            ).rstrip("/")
            if origin not in {"https://api.kiwoom.com", "https://mockapi.kiwoom.com"}:
                raise ValueError("Kiwoom base URL must be an approved official origin")
            self._http = ReadOnlyHttpClient(
                origin,
                {
                    RequestRule("POST", "/oauth2/token"),
                    RequestRule("POST", _ACCOUNT_PATH, _READ_ONLY_TRS),
                    RequestRule("POST", _US_ACCOUNT_PATH, _US_READ_ONLY_TRS),
                },
            )
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        return bool(self._app_key and self._app_secret)

    @property
    def _account_id(self) -> str:
        # The raw account number never enters the process; a stable local alias
        # is sufficient because a Kiwoom App Key is registered to its account.
        return self._local_account_id

    async def _access_token(self) -> str:
        if self._token and self._token_expires_at > now_utc() + timedelta(minutes=5):
            return self._token
        async with self._token_lock:
            if self._token and self._token_expires_at > now_utc() + timedelta(minutes=5):
                return self._token
            payload, _ = await self._http.request_json(
                "POST",
                "/oauth2/token",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._app_secret,
                },
            )
            if payload.get("return_code") not in (None, 0):
                raise RuntimeError("Kiwoom token issuance failed")
            token = str(payload.get("token") or "").strip()
            if not token:
                raise RuntimeError("Kiwoom token response was incomplete")
            self._token = token
            # Official tokens last 24 hours. Keep an extra safety margin and do
            # not persist them to disk.
            self._token_expires_at = now_utc() + timedelta(hours=23)
            return token

    async def _query_pages(self, api_id: str, body: dict[str, str]) -> list[dict]:
        if api_id in _READ_ONLY_TRS:
            path = _ACCOUNT_PATH
        elif api_id in _US_READ_ONLY_TRS:
            path = _US_ACCOUNT_PATH
        else:
            raise ValueError("TR is not a read-only allowlisted query")
        token = await self._access_token()
        pages: list[dict] = []
        cont_yn, next_key = "N", ""
        for _ in range(50):
            payload, headers = await self._http.request_json(
                "POST",
                path,
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {token}",
                    "cont-yn": cont_yn,
                    "next-key": next_key,
                    "api-id": api_id,
                },
                json=body,
            )
            if payload.get("return_code") not in (None, 0):
                raise RuntimeError(f"Kiwoom query failed: {api_id}")
            pages.append(payload)
            if str(headers.get("cont-yn", "N")).upper() != "Y":
                return pages
            next_key = str(headers.get("next-key") or "")
            if not next_key:
                raise RuntimeError("Kiwoom pagination key missing")
            cont_yn = "Y"
        raise RuntimeError("Kiwoom pagination limit exceeded")

    async def get_accounts(self) -> BrokerResult[Account]:
        if not self.configured:
            return not_configured(self.name)
        return BrokerResult.ok(
            self.name,
            [Account(self.name, self._account_id, self._account_alias, Currency.KRW)],
            as_of=now_utc(),
        )

    async def get_positions(self) -> BrokerResult[Position]:
        if not self.configured:
            return not_configured(self.name)
        as_of = now_utc()
        positions: list[Position] = []
        errors: list[BrokerError] = []
        last_failure: BrokerResult[Position] | None = None
        try:
            pages = await self._query_pages("kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
            for page in pages:
                for row in required_list(page, "acnt_evlt_remn_indv_tot"):
                    raw_ticker = str(row.get("stk_cd") or "").strip().upper()
                    ticker = (
                        raw_ticker[1:]
                        if raw_ticker.startswith("A") and raw_ticker[1:].isdigit()
                        else raw_ticker
                    )
                    if not ticker:
                        continue
                    positions.append(
                        Position(
                            broker=self.name,
                            account_id=self._account_id,
                            instrument=InstrumentKey("KRX", ticker, AssetType.EQUITY, Currency.KRW),
                            quantity=decimal_string(row.get("rmnd_qty")),
                            average_price=decimal_string(row.get("pur_pric"), absolute=True),
                            current_price=decimal_string(row.get("cur_prc"), absolute=True),
                            cost_basis=decimal_string(row.get("pur_amt"), absolute=True),
                            market_value=decimal_string(row.get("evlt_amt"), absolute=True),
                            unrealized_profit=decimal_string(row.get("evltv_prft")),
                            return_pct=decimal_string(row.get("prft_rt")),
                            company_name=str(row.get("stk_nm") or "").strip(),
                            as_of=as_of,
                        )
                    )
        except Exception as exc:
            last_failure = failure_result(self.name, exc)
            errors.extend(last_failure.errors)

        if not self._mock:
            try:
                us_pages = await self._query_pages("ust21070", {"stex_tp": "", "stk_cd": ""})
                for page in us_pages:
                    for row in required_list(page, "result_list"):
                        ticker = str(row.get("stk_cd") or "").strip().upper()
                        if not ticker:
                            continue
                        exchange = str(row.get("stex_nm") or "US").strip().upper()
                        market = (
                            "NASDAQ"
                            if "NASDAQ" in exchange or exchange == "NAS"
                            else (
                                "NYSE"
                                if "NYSE" in exchange
                                else ("AMEX" if "AMEX" in exchange else "US")
                            )
                        )
                        currency = Currency(str(row.get("crnc_code") or "USD").upper())
                        positions.append(
                            Position(
                                broker=self.name,
                                account_id=self._account_id,
                                instrument=InstrumentKey(
                                    market, ticker, AssetType.EQUITY, currency
                                ),
                                quantity=decimal_string(row.get("poss_qty")),
                                average_price=decimal_string(
                                    row.get("frgn_stk_book_uv"), absolute=True
                                ),
                                current_price=decimal_string(row.get("now_pric"), absolute=True),
                                cost_basis=decimal_string(
                                    row.get("frgn_stk_book_amt"), absolute=True
                                ),
                                market_value=decimal_string(row.get("evlt_amt"), absolute=True),
                                unrealized_profit=decimal_string(row.get("pl_amt")),
                                return_pct=decimal_string(row.get("pl_rt")),
                                company_name=str(row.get("frgn_stk_nm") or "").strip(),
                                as_of=as_of,
                            )
                        )
            except Exception as exc:
                last_failure = failure_result(self.name, exc)
                errors.extend(last_failure.errors)

        if errors and positions:
            return BrokerResult(
                broker=self.name,
                status=BrokerStatus.PARTIAL,
                items=tuple(positions),
                errors=tuple(errors),
                as_of=as_of,
                missing_accounts=(self._account_id,),
            )
        if errors:
            assert last_failure is not None
            return last_failure
        return BrokerResult.ok(self.name, positions, as_of=as_of)

    async def get_cash(self) -> BrokerResult[CashBalance]:
        if not self.configured:
            return not_configured(self.name)
        as_of = now_utc()
        balances: list[CashBalance] = []
        errors: list[BrokerError] = []
        last_failure: BrokerResult[CashBalance] | None = None
        try:
            pages = await self._query_pages("kt00001", {"qry_tp": "2"})
            if len(pages) != 1:
                raise RuntimeError("unexpected cash pagination")
            balances.append(
                CashBalance(
                    self.name,
                    self._account_id,
                    Currency.KRW,
                    decimal_string(pages[0].get("entr")),
                    as_of,
                )
            )
        except Exception as exc:
            last_failure = failure_result(self.name, exc)
            errors.extend(last_failure.errors)

        if not self._mock:
            try:
                us_pages = await self._query_pages("ust21110", {})
                for page in us_pages:
                    for row in required_list(page, "result_list"):
                        code = str(row.get("crnc_code") or "").strip().upper()
                        if code not in {Currency.KRW.value, Currency.USD.value}:
                            continue
                        balances.append(
                            CashBalance(
                                self.name,
                                self._account_id,
                                Currency(code),
                                decimal_string(row.get("fc_entra")),
                                as_of,
                            )
                        )
            except Exception as exc:
                last_failure = failure_result(self.name, exc)
                errors.extend(last_failure.errors)

        if errors and balances:
            return BrokerResult(
                broker=self.name,
                status=BrokerStatus.PARTIAL,
                items=tuple(balances),
                errors=tuple(errors),
                as_of=as_of,
                missing_accounts=(self._account_id,),
            )
        if errors:
            assert last_failure is not None
            return last_failure
        return BrokerResult.ok(self.name, balances, as_of=as_of)

    async def get_transactions(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> BrokerResult[Transaction]:
        if not self.configured:
            return not_configured(self.name)
        today = datetime.now(_KST).date()
        if start_date > end_date:
            return BrokerResult.failure(
                self.name,
                BrokerStatus.UNAVAILABLE,
                error=BrokerError("INVALID_RANGE", "조회 기간이 올바르지 않습니다."),
            )
        # ka10076 is a same-day execution query. Never pretend it covers older
        # history; storage retains executions collected on previous days.
        if not (start_date <= today <= end_date):
            return BrokerResult.ok(self.name, [], as_of=now_utc())
        transactions: list[Transaction] = []
        errors: list[BrokerError] = []
        last_failure: BrokerResult[Transaction] | None = None
        try:
            pages = await self._query_pages(
                "ka10076",
                {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"},
            )
            for page in pages:
                for row in required_list(page, "cntr"):
                    quantity = decimal_string(row.get("cntr_qty"), absolute=True)
                    if quantity <= 0:
                        continue
                    side = (
                        TransactionSide.BUY
                        if "매수" in str(row.get("io_tp_nm") or "")
                        else TransactionSide.SELL
                    )
                    ticker = str(row.get("stk_cd") or "").strip().upper().removeprefix("A")
                    clock_text = str(row.get("ord_tm") or "").strip().zfill(6)[-6:]
                    try:
                        local_time = time(
                            int(clock_text[:2]), int(clock_text[2:4]), int(clock_text[4:6])
                        )
                        executed_at = datetime.combine(today, local_time, tzinfo=_KST)
                    except ValueError:
                        executed_at = datetime.combine(today, time.min, tzinfo=_KST)
                    order_id = str(row.get("ord_no") or "unknown").strip()
                    transactions.append(
                        Transaction(
                            transaction_id=f"{today.isoformat()}:{order_id}:{len(transactions)}",
                            broker=self.name,
                            account_id=self._account_id,
                            instrument=InstrumentKey("KRX", ticker, AssetType.EQUITY, Currency.KRW),
                            side=side,
                            quantity=quantity,
                            price=decimal_string(row.get("cntr_pric"), absolute=True),
                            gross_amount=quantity
                            * decimal_string(row.get("cntr_pric"), absolute=True),
                            fee=decimal_string(
                                row.get("tdy_trde_cmsn"), absolute=True, default_zero=True
                            ),
                            tax=decimal_string(
                                row.get("tdy_trde_tax"), absolute=True, default_zero=True
                            ),
                            trade_date=today,
                            executed_at=executed_at,
                        )
                    )
        except Exception as exc:
            last_failure = failure_result(self.name, exc)
            errors.extend(last_failure.errors)

        if not self._mock:
            try:
                us_pages = await self._query_pages(
                    "ust21510", {"slby_tp": "0", "stex_tp": "", "stk_cd": ""}
                )
                for page in us_pages:
                    for row in required_list(page, "result_list"):
                        quantity = decimal_string(row.get("cntr_qty"), absolute=True)
                        if quantity <= 0:
                            continue
                        side_text = str(row.get("slby_tp_nm") or row.get("slby_tp") or "")
                        side = (
                            TransactionSide.BUY
                            if ("매수" in side_text or side_text == "2")
                            else TransactionSide.SELL
                        )
                        currency = Currency(str(row.get("crnc_code") or "USD").upper())
                        exchange = str(row.get("stex_nm") or "US").upper()
                        market = (
                            "NASDAQ"
                            if "NASDAQ" in exchange
                            else (
                                "NYSE"
                                if "NYSE" in exchange
                                else ("AMEX" if "AMEX" in exchange else "US")
                            )
                        )
                        clock_text = (
                            str(row.get("cntr_time") or row.get("ord_time") or "")
                            .strip()
                            .replace(":", "")
                        )
                        clock_text = clock_text.zfill(6)[-6:]
                        try:
                            local_time = time(
                                int(clock_text[:2]), int(clock_text[2:4]), int(clock_text[4:6])
                            )
                        except ValueError:
                            local_time = time.min
                        executed_at = datetime.combine(today, local_time, tzinfo=_KST)
                        price = decimal_string(row.get("cntr_uv"), absolute=True)
                        order_id = str(row.get("ord_no") or "unknown").strip()
                        transactions.append(
                            Transaction(
                                transaction_id=f"{today.isoformat()}:US:{order_id}:{len(transactions)}",
                                broker=self.name,
                                account_id=self._account_id,
                                instrument=InstrumentKey(
                                    market,
                                    str(row.get("stk_cd") or "").strip().upper(),
                                    AssetType.EQUITY,
                                    currency,
                                ),
                                side=side,
                                quantity=quantity,
                                price=price,
                                gross_amount=quantity * price,
                                fee=0,
                                tax=0,
                                trade_date=today,
                                executed_at=executed_at,
                            )
                        )
            except Exception as exc:
                last_failure = failure_result(self.name, exc)
                errors.extend(last_failure.errors)

        if errors and transactions:
            return BrokerResult(
                broker=self.name,
                status=BrokerStatus.PARTIAL,
                items=tuple(transactions),
                errors=tuple(errors),
                as_of=now_utc(),
                missing_accounts=(self._account_id,),
            )
        if errors:
            assert last_failure is not None
            return last_failure
        return BrokerResult.ok(self.name, transactions, as_of=now_utc())
