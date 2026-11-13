"""Explicit success and degradation semantics for read-only broker calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Iterable, Optional, Tuple, TypeVar

T = TypeVar("T")


class BrokerStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    STALE = "stale"
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BrokerError:
    """A sanitized error safe for logs and user-facing status summaries."""

    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not str(self.code).strip():
            raise ValueError("error code must not be empty")
        if not str(self.message).strip():
            raise ValueError("error message must not be empty")


@dataclass(frozen=True)
class BrokerResult(Generic[T]):
    broker: str
    status: BrokerStatus
    items: Tuple[T, ...] = ()
    errors: Tuple[BrokerError, ...] = ()
    as_of: Optional[datetime] = None
    missing_accounts: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        broker = str(self.broker).strip().lower()
        if not broker:
            raise ValueError("broker must not be empty")
        object.__setattr__(self, "broker", broker)
        if not isinstance(self.status, BrokerStatus):
            object.__setattr__(self, "status", BrokerStatus(str(self.status).lower()))
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "missing_accounts", tuple(self.missing_accounts))
        if self.status == BrokerStatus.OK and (self.errors or self.missing_accounts):
            raise ValueError("OK result cannot contain errors or missing accounts")
        if self.status == BrokerStatus.PARTIAL and not (self.errors or self.missing_accounts):
            raise ValueError("PARTIAL result must explain what is missing")

    @property
    def data(self) -> Tuple[T, ...]:
        """Compatibility alias for integrations that call the payload data."""

        return self.items

    @property
    def is_complete(self) -> bool:
        return self.status == BrokerStatus.OK

    @property
    def has_usable_data(self) -> bool:
        return bool(self.items) and self.status in {
            BrokerStatus.OK,
            BrokerStatus.PARTIAL,
            BrokerStatus.STALE,
        }

    @classmethod
    def ok(
        cls,
        broker: str,
        items: Iterable[T] = (),
        *,
        as_of: Optional[datetime] = None,
    ) -> "BrokerResult[T]":
        return cls(broker=broker, status=BrokerStatus.OK, items=tuple(items), as_of=as_of)

    @classmethod
    def failure(
        cls,
        broker: str,
        status: BrokerStatus,
        *,
        error: BrokerError,
        items: Iterable[T] = (),
        as_of: Optional[datetime] = None,
        missing_accounts: Iterable[str] = (),
    ) -> "BrokerResult[T]":
        if status == BrokerStatus.OK:
            raise ValueError("failure status cannot be OK")
        return cls(
            broker=broker,
            status=status,
            items=tuple(items),
            errors=(error,),
            as_of=as_of,
            missing_accounts=tuple(missing_accounts),
        )
