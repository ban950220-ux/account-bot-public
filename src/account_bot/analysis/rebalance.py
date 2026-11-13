"""Read-only, new-money-first rebalancing recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Optional, Tuple

from account_bot.domain import (
    Currency,
    InstrumentKey,
    Money,
    as_decimal,
    instruments_compatible,
)
from account_bot.portfolio.models import PortfolioSnapshot


class RebalanceAction(str, Enum):
    ADD_WITH_NEW_MONEY = "add_with_new_money"
    HOLD = "hold"
    OVERWEIGHT_NO_NEW_MONEY = "overweight_no_new_money"


@dataclass(frozen=True)
class TargetAllocation:
    instrument: InstrumentKey
    target_weight_pct: Decimal

    def __post_init__(self) -> None:
        target = as_decimal(self.target_weight_pct, name="target_weight_pct")
        if target < 0 or target > 100:
            raise ValueError("target_weight_pct must be between 0 and 100")
        object.__setattr__(self, "target_weight_pct", target)


@dataclass(frozen=True)
class RebalanceItem:
    instrument: InstrumentKey
    current_value: Money
    current_weight_pct: Optional[Decimal]
    target_weight_pct: Decimal
    suggested_new_money: Money
    projected_weight_pct: Optional[Decimal]
    remaining_gap_pct: Optional[Decimal]
    action: RebalanceAction


@dataclass(frozen=True)
class RebalancePlan:
    is_eligible: bool
    base_currency: Currency
    new_money: Money
    allocated_new_money: Money
    unallocated_cash: Money
    projected_total_value: Optional[Money]
    items: Tuple[RebalanceItem, ...]
    uses_sales: bool
    warnings: Tuple[str, ...] = ()


def suggest_new_money_rebalance(
    snapshot: PortfolioSnapshot,
    targets: Iterable[TargetAllocation],
    new_money: Money,
) -> RebalancePlan:
    """Allocate only new capital to underweight targets; never suggest a sale."""

    if new_money.currency != snapshot.base_currency:
        raise ValueError("new money currency must match portfolio base currency")
    if new_money.amount < 0:
        raise ValueError("new money must not be negative")
    target_tuple = tuple(targets)
    target_map: Dict[InstrumentKey, Decimal] = {}
    for target in target_tuple:
        if target.instrument in target_map:
            raise ValueError(f"duplicate target: {target.instrument.stable_id}")
        target_map[target.instrument] = target.target_weight_pct
    target_sum = sum(target_map.values(), Decimal("0"))
    if target_sum > 100:
        raise ValueError("target weights must not sum to more than 100")

    zero = Money(Decimal("0"), snapshot.base_currency)
    if not snapshot.is_complete or snapshot.total_market_value is None:
        return RebalancePlan(
            is_eligible=False,
            base_currency=snapshot.base_currency,
            new_money=new_money,
            allocated_new_money=zero,
            unallocated_cash=new_money,
            projected_total_value=None,
            items=(),
            uses_sales=False,
            warnings=("rebalance_withheld:portfolio_snapshot_incomplete",),
        )

    current_values: Dict[InstrumentKey, Decimal] = {}
    for position in snapshot.positions:
        if position.market_value_base is None:
            return RebalancePlan(
                is_eligible=False,
                base_currency=snapshot.base_currency,
                new_money=new_money,
                allocated_new_money=zero,
                unallocated_cash=new_money,
                projected_total_value=None,
                items=(),
                uses_sales=False,
                warnings=("rebalance_withheld:position_valuation_missing",),
            )
        current_values[position.instrument] = position.market_value_base

    # Some read-only providers can identify a US security only as ``US:TICKER``.
    # Resolve a configured exchange-specific target onto that holding only when
    # there is exactly one compatible position.  Treating the two keys as
    # different would recommend buying a duplicate position; guessing between
    # multiple compatible listings would be equally unsafe.
    canonical_targets: Dict[InstrumentKey, Decimal] = {}
    alias_warnings = []
    position_instruments = tuple(current_values)
    for target in target_tuple:
        if target.instrument in current_values:
            canonical = target.instrument
        else:
            compatible = tuple(
                instrument
                for instrument in position_instruments
                if instruments_compatible(instrument, target.instrument)
            )
            if len(compatible) > 1:
                return RebalancePlan(
                    is_eligible=False,
                    base_currency=snapshot.base_currency,
                    new_money=new_money,
                    allocated_new_money=zero,
                    unallocated_cash=new_money,
                    projected_total_value=None,
                    items=(),
                    uses_sales=False,
                    warnings=("rebalance_withheld:ambiguous_instrument_alias",),
                )
            canonical = compatible[0] if compatible else target.instrument
        if canonical in canonical_targets:
            raise ValueError(f"duplicate compatible target: {canonical.stable_id}")
        canonical_targets[canonical] = target.target_weight_pct
        if canonical != target.instrument:
            alias_warnings.append(
                f"instrument_alias_resolved:{target.instrument.display_id}->{canonical.display_id}"
            )
    target_map = canonical_targets

    current_total = snapshot.total_market_value.amount
    projected_total = current_total + new_money.amount
    all_instruments = set(current_values) | set(target_map)
    deficits: Dict[InstrumentKey, Decimal] = {}
    for instrument in all_instruments:
        target_value = projected_total * target_map.get(instrument, Decimal("0")) / Decimal("100")
        deficits[instrument] = max(
            target_value - current_values.get(instrument, Decimal("0")), Decimal("0")
        )
    total_deficit = sum(deficits.values(), Decimal("0"))
    available_for_targets = min(new_money.amount, total_deficit)
    allocations = {instrument: Decimal("0") for instrument in all_instruments}
    if total_deficit > 0 and available_for_targets > 0:
        if total_deficit <= available_for_targets:
            allocations.update(deficits)
        else:
            for instrument, deficit in deficits.items():
                allocations[instrument] = available_for_targets * deficit / total_deficit
            # Decimal division can leave a microscopic context-rounding residual.
            residual = available_for_targets - sum(allocations.values(), Decimal("0"))
            if residual:
                largest = sorted(
                    deficits,
                    key=lambda key: (-deficits[key], key.stable_id),
                )[0]
                allocations[largest] += residual

    allocated = sum(allocations.values(), Decimal("0"))
    unallocated = new_money.amount - allocated
    items = []
    for instrument in sorted(all_instruments, key=lambda value: value.stable_id):
        current_value = current_values.get(instrument, Decimal("0"))
        target_weight = target_map.get(instrument, Decimal("0"))
        suggestion = allocations[instrument]
        current_weight = (
            current_value / current_total * Decimal("100") if current_total > 0 else None
        )
        projected_weight = (
            (current_value + suggestion) / projected_total * Decimal("100")
            if projected_total > 0
            else None
        )
        remaining_gap = target_weight - projected_weight if projected_weight is not None else None
        if suggestion > 0:
            action = RebalanceAction.ADD_WITH_NEW_MONEY
        elif current_weight is not None and current_weight > target_weight:
            action = RebalanceAction.OVERWEIGHT_NO_NEW_MONEY
        else:
            action = RebalanceAction.HOLD
        items.append(
            RebalanceItem(
                instrument=instrument,
                current_value=Money(current_value, snapshot.base_currency),
                current_weight_pct=current_weight,
                target_weight_pct=target_weight,
                suggested_new_money=Money(suggestion, snapshot.base_currency),
                projected_weight_pct=projected_weight,
                remaining_gap_pct=remaining_gap,
                action=action,
            )
        )
    return RebalancePlan(
        is_eligible=True,
        base_currency=snapshot.base_currency,
        new_money=new_money,
        allocated_new_money=Money(allocated, snapshot.base_currency),
        unallocated_cash=Money(unallocated, snapshot.base_currency),
        projected_total_value=Money(projected_total, snapshot.base_currency),
        items=tuple(items),
        uses_sales=False,
        warnings=("new_money_first:no_sales_suggested", *alias_warnings),
    )
