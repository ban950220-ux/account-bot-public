"""Normalization helpers shared by broker adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

import httpx

from account_bot.domain import BrokerError, BrokerResult, BrokerStatus

from .http import BrokerHttpError

T = TypeVar("T")


def decimal_string(
    value: Any,
    *,
    absolute: bool = False,
    default_zero: bool = False,
) -> Decimal:
    if value is None or not str(value).strip():
        if default_zero:
            return Decimal("0")
        raise ValueError("broker response omitted a required decimal field")
    text = str(value).strip().replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("broker returned an invalid decimal field") from exc
    if not number.is_finite():
        raise ValueError("broker returned a non-finite decimal field")
    return abs(number) if absolute else number


def required_mapping(container: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"broker response omitted required object: {key}")
    return value


def required_list(container: Mapping[str, Any], key: str) -> list[Any]:
    value = container.get(key)
    if not isinstance(value, list):
        raise ValueError(f"broker response omitted required list: {key}")
    return value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def failure_result(broker: str, exc: Exception) -> BrokerResult[T]:
    if isinstance(exc, BrokerHttpError):
        if exc.status_code in {401, 403}:
            status, code, retryable = BrokerStatus.AUTH_FAILED, "AUTH_FAILED", False
        elif exc.status_code == 429:
            status, code, retryable = BrokerStatus.RATE_LIMITED, "RATE_LIMITED", True
        else:
            status, code, retryable = BrokerStatus.UNAVAILABLE, "HTTP_ERROR", exc.status_code >= 500
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        status, code, retryable = BrokerStatus.UNAVAILABLE, "TIMEOUT", True
    elif isinstance(exc, httpx.TransportError):
        status, code, retryable = BrokerStatus.UNAVAILABLE, "TRANSPORT_ERROR", True
    else:
        status, code, retryable = BrokerStatus.UNAVAILABLE, type(exc).__name__.upper(), False
    return BrokerResult.failure(
        broker,
        status,
        error=BrokerError(code=code, message="증권사 조회에 실패했습니다.", retryable=retryable),
    )


def not_configured(
    broker: str, message: str = "증권사 인증정보가 설정되지 않았습니다."
) -> BrokerResult[T]:
    return BrokerResult.failure(
        broker,
        BrokerStatus.NOT_CONFIGURED,
        error=BrokerError("NOT_CONFIGURED", message),
    )
