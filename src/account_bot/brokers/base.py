"""Common interface implemented by every brokerage adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from account_bot.domain import Account, BrokerResult, CashBalance, Position, Transaction


class ReadOnlyBroker(ABC):
    """The complete broker surface intentionally contains no write operation."""

    name: str

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_accounts(self) -> BrokerResult[Account]:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> BrokerResult[Position]:
        raise NotImplementedError

    @abstractmethod
    async def get_cash(self) -> BrokerResult[CashBalance]:
        raise NotImplementedError

    @abstractmethod
    async def get_transactions(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> BrokerResult[Transaction]:
        """Return executions in an inclusive broker-local date range."""

        raise NotImplementedError

    async def aclose(self) -> None:
        """Release optional network resources; snapshot adapters need no action."""

        return None
