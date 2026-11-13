"""Reconcile recurring-buy intentions with read-only transaction history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional, Tuple

from account_bot.domain import (
    InstrumentKey,
    Money,
    Transaction,
    TransactionSide,
    as_decimal,
    instruments_compatible,
)


class RecurringBuyStatus(str, Enum):
    DISABLED = "disabled"
    NOT_DUE = "not_due"
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    MISSING = "missing"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class RecurringBuyPlan:
    instrument: InstrumentKey
    daily_amount: Money
    enabled: bool = True
    target_weight_pct: Optional[Decimal] = None
    broker: Optional[str] = None
    account_id: Optional[str] = None
    scheduled_weekdays: Tuple[int, ...] = (0, 1, 2, 3, 4)
    investment_thesis: str = ""
    memo: str = ""
    started_at: Optional[date] = None

    def __post_init__(self) -> None:
        # Fractional overseas recurring buys can be funded in KRW while the
        # traded instrument and resulting execution are denominated in USD.
        # Reconciliation must withhold a fill judgment unless a conversion is
        # available, but the plan itself remains valid and displayable.
        if self.daily_amount.amount <= 0:
            raise ValueError("daily amount must be positive")
        if self.target_weight_pct is not None:
            target = as_decimal(self.target_weight_pct, name="target_weight_pct")
            if target < 0 or target > 100:
                raise ValueError("target_weight_pct must be between 0 and 100")
            object.__setattr__(self, "target_weight_pct", target)
        if self.broker is not None:
            broker = str(self.broker).strip().lower()
            if not broker:
                raise ValueError("broker must not be blank")
            object.__setattr__(self, "broker", broker)
        if self.account_id is not None and not str(self.account_id).strip():
            raise ValueError("account_id must not be blank")
        weekdays = tuple(sorted(set(self.scheduled_weekdays)))
        if any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("scheduled weekdays must be between 0 and 6")
        object.__setattr__(self, "scheduled_weekdays", weekdays)


@dataclass(frozen=True)
class RecurringBuyEvaluation:
    status: RecurringBuyStatus
    expected: Money
    actual: Money
    remaining: Money
    matched_transaction_ids: Tuple[str, ...]
    is_final: bool
    reason: str


def evaluate_recurring_buy(
    plan: RecurringBuyPlan,
    transactions: Iterable[Transaction],
    *,
    target_date: date,
    now: datetime,
    cutoff_at: datetime,
    data_complete: bool = True,
    is_trading_day: bool = True,
    tolerance_pct: Decimal = Decimal("1"),
    absolute_tolerance: Decimal = Decimal("0"),
) -> RecurringBuyEvaluation:
    """Classify fills without treating pre-cutoff or API failures as missing."""

    tolerance_pct = as_decimal(tolerance_pct, name="tolerance_pct")
    absolute_tolerance = as_decimal(absolute_tolerance, name="absolute_tolerance")
    if tolerance_pct < 0 or absolute_tolerance < 0:
        raise ValueError("tolerances must not be negative")
    try:
        cutoff_passed = now >= cutoff_at
    except TypeError as exc:
        raise ValueError("now and cutoff_at must use compatible timezone awareness") from exc

    def result(
        status: RecurringBuyStatus,
        actual_amount: Decimal,
        ids: Tuple[str, ...],
        *,
        final: bool,
        reason: str,
    ) -> RecurringBuyEvaluation:
        remaining = max(plan.daily_amount.amount - actual_amount, Decimal("0"))
        return RecurringBuyEvaluation(
            status=status,
            expected=plan.daily_amount,
            actual=Money(actual_amount, plan.daily_amount.currency),
            remaining=Money(remaining, plan.daily_amount.currency),
            matched_transaction_ids=ids,
            is_final=final,
            reason=reason,
        )

    if not plan.enabled:
        return result(
            RecurringBuyStatus.DISABLED,
            Decimal("0"),
            (),
            final=True,
            reason="recurring buy is disabled",
        )
    if (
        (plan.started_at is not None and target_date < plan.started_at)
        or target_date.weekday() not in plan.scheduled_weekdays
        or not is_trading_day
    ):
        return result(
            RecurringBuyStatus.NOT_DUE,
            Decimal("0"),
            (),
            final=True,
            reason="not a scheduled trading day",
        )
    matching = []
    for transaction in transactions:
        if transaction.side != TransactionSide.BUY:
            continue
        if (
            not instruments_compatible(transaction.instrument, plan.instrument)
            or transaction.trade_date != target_date
        ):
            continue
        if plan.broker is not None and transaction.broker != plan.broker:
            continue
        if plan.account_id is not None and transaction.account_id != plan.account_id:
            continue
        matching.append(transaction)
    actual_amount = sum((transaction.gross_amount for transaction in matching), Decimal("0"))
    ids = tuple(sorted(transaction.transaction_id for transaction in matching))
    if not data_complete:
        return result(
            RecurringBuyStatus.UNVERIFIABLE,
            actual_amount,
            ids,
            final=False,
            reason="broker transaction data is incomplete",
        )
    tolerance = max(
        absolute_tolerance,
        plan.daily_amount.amount * tolerance_pct / Decimal("100"),
    )
    if actual_amount >= plan.daily_amount.amount - tolerance:
        return result(
            RecurringBuyStatus.FILLED,
            actual_amount,
            ids,
            final=True,
            reason="executed amount is within the configured tolerance",
        )
    if actual_amount > 0:
        return result(
            RecurringBuyStatus.PARTIALLY_FILLED,
            actual_amount,
            ids,
            final=cutoff_passed,
            reason=(
                "partial execution; cutoff passed"
                if cutoff_passed
                else "partial execution; more fills may arrive before cutoff"
            ),
        )
    if not cutoff_passed:
        return result(
            RecurringBuyStatus.PENDING,
            Decimal("0"),
            (),
            final=False,
            reason="execution cutoff has not passed",
        )
    return result(
        RecurringBuyStatus.MISSING,
        Decimal("0"),
        (),
        final=True,
        reason="no matching execution after cutoff",
    )
