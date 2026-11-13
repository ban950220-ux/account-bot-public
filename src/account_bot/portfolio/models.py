"""Immutable output models produced by portfolio aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from account_bot.domain import BrokerStatus, Currency, InstrumentKey, Money


@dataclass(frozen=True)
class BrokerQuantity:
    broker: str
    quantity: Decimal


@dataclass(frozen=True)
class AggregatedPosition:
    instrument: InstrumentKey
    company_name: str
    quantity: Decimal
    average_price: Optional[Decimal]
    current_price: Optional[Decimal]
    cost_basis: Optional[Decimal]
    market_value: Optional[Decimal]
    unrealized_profit: Optional[Decimal]
    return_pct: Optional[Decimal]
    cost_basis_base: Optional[Decimal]
    market_value_base: Optional[Decimal]
    weight_pct: Optional[Decimal]
    broker_quantities: Tuple[BrokerQuantity, ...]


@dataclass(frozen=True)
class ConvertedCashBalance:
    broker: str
    account_id: str
    currency: Currency
    amount: Decimal
    amount_base: Optional[Decimal]


@dataclass(frozen=True)
class PortfolioSnapshot:
    """A coherent view of all expected broker results.

    ``included_market_value`` is a clearly labelled subtotal of values that were
    usable.  ``total_market_value`` and all weights exist only when every
    expected read succeeded and every valuation could be converted.
    """

    base_currency: Currency
    positions: Tuple[AggregatedPosition, ...]
    cash_balances: Tuple[ConvertedCashBalance, ...]
    included_position_value: Money
    included_cash_value: Money
    included_market_value: Money
    total_market_value: Optional[Money]
    included_cost_basis: Optional[Money]
    total_cost_basis: Optional[Money]
    cash_weight_pct: Optional[Decimal]
    is_complete: bool
    includes_cash: bool
    expected_brokers: Tuple[str, ...]
    included_brokers: Tuple[str, ...]
    incomplete_brokers: Tuple[str, ...]
    position_statuses: Tuple[Tuple[str, BrokerStatus], ...]
    cash_statuses: Tuple[Tuple[str, BrokerStatus], ...]
    warnings: Tuple[str, ...]

    @property
    def weights_available(self) -> bool:
        return (
            self.is_complete
            and self.total_market_value is not None
            and self.total_market_value.amount > 0
        )
