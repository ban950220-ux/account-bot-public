"""Strict normalized JSON snapshot adapter for brokers without a public API."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from account_bot.domain import (
    Account,
    AssetType,
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

_FORBIDDEN_KEYS = {
    "account_number",
    "account_no",
    "api_key",
    "api_secret",
    "access_token",
    "refresh_token",
    "password",
    "authorization",
    "client_secret",
    "credential",
    "pin",
}
_FORBIDDEN_NORMALIZED_KEYS = frozenset(re.sub(r"[^a-z0-9]", "", key) for key in _FORBIDDEN_KEYS)


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if (
                normalized_key in _FORBIDDEN_NORMALIZED_KEYS
                or normalized_key.endswith("secret")
                or normalized_key.endswith("token")
            ):
                raise ValueError("snapshot contains a forbidden sensitive field")
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


def _account_alias(value: Any) -> str:
    alias = str(value).strip()
    if not alias:
        raise ValueError("snapshot account_id must be a local alias")
    if re.fullmatch(r"\d[\d-]{5,}", alias):
        raise ValueError("snapshot account_id must not be a raw account number")
    return alias


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("snapshot datetime must be ISO-8601") from exc
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


class JsonSnapshotBroker(ReadOnlyBroker):
    """Reads an explicitly normalized export; it never scrapes a broker app."""

    def __init__(
        self,
        broker: str,
        snapshot_path: Path | None,
        *,
        maximum_age: timedelta = timedelta(days=3),
    ) -> None:
        if maximum_age <= timedelta(0):
            raise ValueError("maximum snapshot age must be positive")
        self.name = broker.strip().lower()
        # A directory is convenient for a manually maintained source: the
        # normalized file always lives at <directory>/snapshot.json.
        self._path = (
            snapshot_path / "snapshot.json"
            if snapshot_path is not None and snapshot_path.is_dir()
            else snapshot_path
        )
        self._maximum_age = maximum_age
        self._payload: dict[str, Any] | None = None
        self._file_as_of: datetime | None = None

    @property
    def configured(self) -> bool:
        return self._path is not None

    async def _load(self) -> dict[str, Any]:
        if self._path is None:
            raise FileNotFoundError("snapshot path is not configured")

        def read() -> tuple[dict[str, Any], datetime]:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("snapshot root must be an object")
            _reject_sensitive_keys(payload)
            modified_at = datetime.fromtimestamp(self._path.stat().st_mtime, timezone.utc)
            return payload, modified_at

        self._payload, self._file_as_of = await asyncio.to_thread(read)
        return self._payload

    def _source_as_of(
        self,
        payload: dict[str, Any],
        row_times: tuple[datetime | None, ...] = (),
    ) -> datetime:
        candidates = [value for value in row_times if value is not None]
        top_level = _parse_datetime(payload.get("as_of"))
        if top_level is not None:
            candidates.append(top_level)
        if self._file_as_of is not None:
            candidates.append(self._file_as_of)
        if not candidates:
            raise ValueError("snapshot freshness could not be determined")
        return min(candidates)

    def _result(self, items: list[Any], *, as_of: datetime) -> BrokerResult[Any]:
        now = now_utc()
        stale = now - as_of > self._maximum_age or as_of - now > timedelta(minutes=5)
        return BrokerResult(
            broker=self.name,
            status=BrokerStatus.STALE if stale else BrokerStatus.OK,
            items=tuple(items),
            as_of=as_of,
        )

    async def get_accounts(self) -> BrokerResult[Account]:
        if not self.configured:
            return not_configured(self.name, "정규화된 수동 스냅샷 경로가 설정되지 않았습니다.")
        try:
            payload = await self._load()
            accounts = [
                Account(
                    self.name,
                    _account_alias(row["account_id"]),
                    str(row.get("display_name") or f"{self.name} 계좌"),
                    Currency(str(row.get("base_currency") or "KRW").upper()),
                )
                for row in required_list(payload, "accounts")
            ]
            return self._result(accounts, as_of=self._source_as_of(payload))
        except Exception as exc:
            return failure_result(self.name, exc)

    async def get_positions(self) -> BrokerResult[Position]:
        if not self.configured:
            return not_configured(self.name, "정규화된 수동 스냅샷 경로가 설정되지 않았습니다.")
        try:
            payload = await self._load()
            rows: list[Position] = []
            for row in required_list(payload, "positions"):
                currency = Currency(str(row.get("currency") or "KRW").upper())
                rows.append(
                    Position(
                        broker=self.name,
                        account_id=_account_alias(row["account_id"]),
                        instrument=InstrumentKey(
                            str(row["market"]),
                            str(row["ticker"]),
                            AssetType(str(row.get("asset_type") or "EQUITY").upper()),
                            currency,
                        ),
                        quantity=decimal_string(row.get("quantity")),
                        average_price=(
                            decimal_string(row["average_price"])
                            if row.get("average_price") is not None
                            else None
                        ),
                        current_price=(
                            decimal_string(row["current_price"])
                            if row.get("current_price") is not None
                            else None
                        ),
                        cost_basis=(
                            decimal_string(row["cost_basis"])
                            if row.get("cost_basis") is not None
                            else None
                        ),
                        market_value=(
                            decimal_string(row["market_value"])
                            if row.get("market_value") is not None
                            else None
                        ),
                        company_name=str(row.get("company_name") or ""),
                        as_of=_parse_datetime(row.get("as_of")),
                    )
                )
            return self._result(
                rows,
                as_of=self._source_as_of(payload, tuple(position.as_of for position in rows)),
            )
        except Exception as exc:
            return failure_result(self.name, exc)

    async def get_cash(self) -> BrokerResult[CashBalance]:
        if not self.configured:
            return not_configured(self.name, "정규화된 수동 스냅샷 경로가 설정되지 않았습니다.")
        try:
            payload = await self._load()
            rows = [
                CashBalance(
                    self.name,
                    _account_alias(row["account_id"]),
                    Currency(str(row.get("currency") or "KRW").upper()),
                    decimal_string(row.get("amount")),
                    _parse_datetime(row.get("as_of")),
                )
                for row in required_list(payload, "cash")
            ]
            return self._result(
                rows,
                as_of=self._source_as_of(payload, tuple(balance.as_of for balance in rows)),
            )
        except Exception as exc:
            return failure_result(self.name, exc)

    async def get_transactions(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> BrokerResult[Transaction]:
        if not self.configured:
            return not_configured(self.name, "정규화된 수동 스냅샷 경로가 설정되지 않았습니다.")
        try:
            payload = await self._load()
            rows: list[Transaction] = []
            for row in required_list(payload, "transactions"):
                trade_date = date.fromisoformat(str(row["trade_date"]))
                if not (start_date <= trade_date <= end_date):
                    continue
                currency = Currency(str(row.get("currency") or "KRW").upper())
                rows.append(
                    Transaction(
                        transaction_id=str(row["transaction_id"]),
                        broker=self.name,
                        account_id=_account_alias(row["account_id"]),
                        instrument=InstrumentKey(
                            str(row["market"]), str(row["ticker"]), AssetType.EQUITY, currency
                        ),
                        side=TransactionSide(str(row["side"]).upper()),
                        quantity=decimal_string(row.get("quantity")),
                        price=decimal_string(row.get("price")),
                        gross_amount=decimal_string(row.get("gross_amount")),
                        fee=decimal_string(row.get("fee")),
                        tax=decimal_string(row.get("tax")),
                        trade_date=trade_date,
                        executed_at=_parse_datetime(row.get("executed_at")),
                    )
                )
            return self._result(rows, as_of=self._source_as_of(payload))
        except Exception as exc:
            return failure_result(self.name, exc)
