"""Immutable value objects shared by brokers, portfolio, and analysis code.

All numeric input deliberately rejects ``float``.  Broker adapters should parse
the provider's decimal strings directly so account calculations never inherit
binary floating-point error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Union

DecimalInput = Union[Decimal, int, str]
US_MARKET_ALIASES = frozenset({"US", "USA", "NASDAQ", "NAS", "NYSE", "AMEX", "ARCA", "NYSEARCA"})


def as_decimal(value: DecimalInput, *, name: str = "value") -> Decimal:
    """Return a finite ``Decimal`` while refusing ambiguous float input."""

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must be Decimal, int, or str; float is not allowed")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(value)
        except Exception as exc:  # Decimal raises several input-specific errors.
            raise ValueError(f"{name} is not a valid decimal") from exc
    else:
        raise TypeError(f"{name} must be Decimal, int, or str")
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


class Currency(str, Enum):
    KRW = "KRW"
    USD = "USD"


class AssetType(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    FUND = "FUND"
    BOND = "BOND"
    CASH = "CASH"
    OTHER = "OTHER"


class TransactionSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    FEE = "FEE"
    TAX = "TAX"
    FX = "FX"


def _currency(value: Union[Currency, str]) -> Currency:
    if isinstance(value, Currency):
        return value
    try:
        return Currency(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError(f"unsupported currency: {value!r}") from exc


def _asset_type(value: Union[AssetType, str]) -> AssetType:
    if isinstance(value, AssetType):
        return value
    try:
        return AssetType(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError(f"unsupported asset type: {value!r}") from exc


def _non_empty(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


@dataclass(frozen=True, order=True)
class InstrumentKey:
    """Identity of a listing, not merely a display ticker."""

    market: str
    ticker: str
    asset_type: AssetType = AssetType.EQUITY
    currency: Currency = Currency.USD

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", _non_empty(self.market, "market").upper())
        object.__setattr__(self, "ticker", _non_empty(self.ticker, "ticker").upper())
        object.__setattr__(self, "asset_type", _asset_type(self.asset_type))
        object.__setattr__(self, "currency", _currency(self.currency))

    @property
    def stable_id(self) -> str:
        return f"{self.market}:{self.ticker}:{self.asset_type.value}:{self.currency.value}"

    @property
    def display_id(self) -> str:
        return f"{self.market}:{self.ticker}"


def instruments_compatible(left: InstrumentKey, right: InstrumentKey) -> bool:
    """Match a generic US market only when a provider lacks exchange detail.

    Exact identity remains market+ticker+type+currency. This helper never
    collapses two known exchanges; only ``US``/``USA`` acts as a wildcard.
    """

    if left == right:
        return True
    if (
        left.ticker != right.ticker
        or left.asset_type != right.asset_type
        or left.currency != right.currency
    ):
        return False
    left_market = left.market.upper()
    right_market = right.market.upper()
    return (left_market in {"US", "USA"} and right_market in US_MARKET_ALIASES) or (
        right_market in {"US", "USA"} and left_market in US_MARKET_ALIASES
    )


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", as_decimal(self.amount, name="amount"))
        object.__setattr__(self, "currency", _currency(self.currency))

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError("cannot add money in different currencies")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError("cannot subtract money in different currencies")
        return Money(self.amount - other.amount, self.currency)


@dataclass(frozen=True)
class FxQuote:
    """A quote expressed as ``quote currency per one base currency``."""

    base: Currency
    quote: Currency
    rate: Decimal
    observed_at: Optional[datetime] = None
    is_stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", _currency(self.base))
        object.__setattr__(self, "quote", _currency(self.quote))
        rate = as_decimal(self.rate, name="rate")
        if rate <= 0:
            raise ValueError("rate must be positive")
        if self.base == self.quote:
            raise ValueError("FX base and quote currencies must differ")
        object.__setattr__(self, "rate", rate)

    def convert(self, money: Money, target: Union[Currency, str]) -> Money:
        target_currency = _currency(target)
        if money.currency == target_currency:
            return money
        if money.currency == self.base and target_currency == self.quote:
            return Money(money.amount * self.rate, target_currency)
        if money.currency == self.quote and target_currency == self.base:
            return Money(money.amount / self.rate, target_currency)
        raise ValueError(
            f"quote {self.base.value}/{self.quote.value} cannot convert "
            f"{money.currency.value} to {target_currency.value}"
        )


@dataclass(frozen=True)
class Account:
    broker: str
    account_id: str
    display_name: str = ""
    base_currency: Currency = Currency.KRW
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker", _non_empty(self.broker, "broker").lower())
        object.__setattr__(self, "account_id", _non_empty(self.account_id, "account_id"))
        object.__setattr__(self, "display_name", str(self.display_name).strip())
        object.__setattr__(self, "base_currency", _currency(self.base_currency))


@dataclass(frozen=True)
class CashBalance:
    """Settled or broker-reported cash available in one account currency."""

    broker: str
    account_id: str
    currency: Currency
    amount: Decimal
    as_of: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker", _non_empty(self.broker, "broker").lower())
        object.__setattr__(self, "account_id", _non_empty(self.account_id, "account_id"))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", as_decimal(self.amount, name="amount"))


@dataclass(frozen=True)
class Position:
    """A broker-normalized long position in its listing currency."""

    broker: str
    account_id: str
    instrument: InstrumentKey
    quantity: Decimal
    average_price: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    cost_basis: Optional[Decimal] = None
    market_value: Optional[Decimal] = None
    unrealized_profit: Optional[Decimal] = None
    return_pct: Optional[Decimal] = None
    company_name: str = ""
    as_of: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker", _non_empty(self.broker, "broker").lower())
        object.__setattr__(self, "account_id", _non_empty(self.account_id, "account_id"))
        if not isinstance(self.instrument, InstrumentKey):
            raise TypeError("instrument must be an InstrumentKey")
        quantity = as_decimal(self.quantity, name="quantity")
        if quantity < 0:
            raise ValueError("quantity must not be negative")
        object.__setattr__(self, "quantity", quantity)
        for field_name in ("average_price", "current_price", "cost_basis", "market_value"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = as_decimal(value, name=field_name)
                if normalized < 0:
                    raise ValueError(f"{field_name} must not be negative")
                object.__setattr__(self, field_name, normalized)
        for field_name in ("unrealized_profit", "return_pct"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, as_decimal(value, name=field_name))
        object.__setattr__(self, "company_name", str(self.company_name).strip())

    @property
    def effective_cost_basis(self) -> Optional[Decimal]:
        if self.cost_basis is not None:
            return self.cost_basis
        if self.average_price is not None:
            return self.average_price * self.quantity
        return None

    @property
    def effective_market_value(self) -> Optional[Decimal]:
        if self.market_value is not None:
            return self.market_value
        if self.current_price is not None:
            return self.current_price * self.quantity
        return None

    @property
    def effective_unrealized_profit(self) -> Optional[Decimal]:
        if self.unrealized_profit is not None:
            return self.unrealized_profit
        cost = self.effective_cost_basis
        value = self.effective_market_value
        if cost is None or value is None:
            return None
        return value - cost

    @property
    def effective_return_pct(self) -> Optional[Decimal]:
        if self.return_pct is not None:
            return self.return_pct
        cost = self.effective_cost_basis
        profit = self.effective_unrealized_profit
        if cost is None or profit is None or cost == 0:
            return None
        return profit / cost * Decimal("100")


@dataclass(frozen=True)
class Transaction:
    """A normalized, read-only broker transaction record."""

    transaction_id: str
    broker: str
    account_id: str
    instrument: InstrumentKey
    side: TransactionSide
    quantity: Decimal
    price: Decimal
    trade_date: date
    gross_amount: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    executed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_id", _non_empty(self.transaction_id, "transaction_id")
        )
        object.__setattr__(self, "broker", _non_empty(self.broker, "broker").lower())
        object.__setattr__(self, "account_id", _non_empty(self.account_id, "account_id"))
        if not isinstance(self.instrument, InstrumentKey):
            raise TypeError("instrument must be an InstrumentKey")
        if not isinstance(self.side, TransactionSide):
            try:
                object.__setattr__(self, "side", TransactionSide(str(self.side).upper()))
            except ValueError as exc:
                raise ValueError(f"unsupported transaction side: {self.side!r}") from exc
        quantity = as_decimal(self.quantity, name="quantity")
        price = as_decimal(self.price, name="price")
        if quantity < 0 or price < 0:
            raise ValueError("quantity and price must not be negative")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        gross = self.gross_amount
        if gross is None:
            gross = quantity * price
        gross_decimal = as_decimal(gross, name="gross_amount")
        if gross_decimal < 0:
            raise ValueError("gross_amount must not be negative")
        object.__setattr__(self, "gross_amount", gross_decimal)
        for field_name in ("fee", "tax"):
            normalized = as_decimal(getattr(self, field_name), name=field_name)
            if normalized < 0:
                raise ValueError(f"{field_name} must not be negative")
            object.__setattr__(self, field_name, normalized)

    @property
    def currency(self) -> Currency:
        return self.instrument.currency
