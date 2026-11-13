"""Read-only recurring budget comparison and explicit amount simulation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from account_bot.domain import Currency

from .recurring import RecurringBuyPlan


@dataclass(frozen=True, slots=True)
class RecurringAllocationSignal:
    exposure: str
    suitability_score: Decimal | None = None
    valuation_score: Decimal | None = None
    has_hard_warning: bool = False


@dataclass(frozen=True, slots=True)
class RecurringAmountRecommendation:
    plan: RecurringBuyPlan
    recommended_amount: Decimal
    action: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecurringBudgetSummary:
    currency: Currency
    current_weekly_amount: Decimal
    recommended_weekly_amount: Decimal
    minimum_weekly_amount: Decimal
    maximum_weekly_amount: Decimal
    # Legacy field names: True means the proposed total hit a rejecting bound.
    # The requested amounts are never silently clamped or persisted.
    cap_applied: bool
    floor_applied: bool
    feasible: bool = True
    status: str = "현재 예산 유지"


def _step(currency: Currency) -> Decimal:
    return Decimal("1000") if currency == Currency.KRW else Decimal("1")


def _minimum_amount(plan: RecurringBuyPlan) -> Decimal:
    if plan.daily_amount.currency != Currency.KRW:
        return _step(plan.daily_amount.currency)
    return Decimal("1000") if plan.broker == "meritz" else Decimal("2000")


def _plan_key(plan: RecurringBuyPlan) -> tuple[str, str | None, str | None]:
    return (plan.instrument.stable_id, plan.broker, plan.account_id)


def recommend_recurring_amounts(
    plans: Iterable[RecurringBuyPlan],
    signals: Mapping[tuple[str, str | None], RecurringAllocationSignal],
    *,
    maximum_total_increase_pct: Decimal = Decimal("5"),
    maximum_total_decrease_pct: Decimal = Decimal("10"),
    requested_amounts: Mapping[tuple[str, str | None, str | None], Decimal] | None = None,
    preserve_weekly_budget: bool = True,
) -> tuple[tuple[RecurringAmountRecommendation, ...], tuple[RecurringBudgetSummary, ...]]:
    """Keep current amounts unless a complete, explicit simulation is feasible.

    Weekly sums use scheduled weekdays, not actual exchange trading days. A
    request never changes a stored plan and never silently rounds a proposed
    amount or converts currencies.
    """

    if not maximum_total_increase_pct.is_finite() or maximum_total_increase_pct < 0:
        raise ValueError("maximum_total_increase_pct must not be negative")
    if (not maximum_total_decrease_pct.is_finite() or
            maximum_total_decrease_pct < 0 or maximum_total_decrease_pct > 100):
        raise ValueError("maximum_total_decrease_pct must be between 0 and 100")
    plans_tuple = tuple(plans)
    enabled = tuple(plan for plan in plans_tuple if plan.enabled)
    weekly_by_currency: dict[Currency, Decimal] = {}
    for plan in enabled:
        currency = plan.daily_amount.currency
        weekly = plan.daily_amount.amount * Decimal(len(plan.scheduled_weekdays))
        weekly_by_currency[currency] = weekly_by_currency.get(currency, Decimal("0")) + weekly
    del signals  # Sector/source comparisons are rendered separately, never optimization targets.
    proposed: dict[int, Decimal] = {id(plan): plan.daily_amount.amount for plan in enabled}
    statuses: dict[Currency, str] = {}
    if requested_amounts is not None:
        keys = [_plan_key(plan) for plan in enabled]
        if len(set(keys)) != len(keys) or set(requested_amounts) != set(keys):
            raise ValueError("simulation requires one unambiguous amount per enabled plan")
        for plan in enabled:
            try:
                amount = Decimal(str(requested_amounts[_plan_key(plan)]))
            except (InvalidOperation, TypeError, ValueError):
                statuses[plan.daily_amount.currency] = "실현 불가: 숫자 형식 오류"
                continue
            currency = plan.daily_amount.currency
            step = _step(currency)
            if not plan.scheduled_weekdays and amount != plan.daily_amount.amount:
                statuses[currency] = "실현 불가: 예정 요일 없음"
            elif not amount.is_finite() or amount < _minimum_amount(plan) or amount % step:
                statuses[currency] = "실현 불가: 최소금액·증액 단위 위반"
            else:
                proposed[id(plan)] = amount

    summaries: list[RecurringBudgetSummary] = []
    for currency, current_total in sorted(weekly_by_currency.items(), key=lambda item: item[0].value):
        group = tuple(plan for plan in enabled if plan.daily_amount.currency == currency)
        requested_total = sum(
            (
                proposed[id(plan)] * Decimal(len(plan.scheduled_weekdays))
                for plan in group
            ),
            Decimal("0"),
        )
        minimum_total = current_total * (Decimal("1") - maximum_total_decrease_pct / 100)
        maximum_total = current_total * (Decimal("1") + maximum_total_increase_pct / 100)
        cap_blocked = requested_amounts is not None and requested_total > maximum_total
        floor_blocked = requested_amounts is not None and requested_total < minimum_total
        status = statuses.get(currency)
        if requested_amounts is not None and status is None:
            if cap_blocked or floor_blocked:
                status = "실현 불가: 주간 예산 상하한 위반"
            elif preserve_weekly_budget and requested_total != current_total:
                status = "실현 불가: 주간 예산 보존 위반"
            else:
                status = "명시 금액안 시뮬레이션 가능"
        feasible = status is None or status == "명시 금액안 시뮬레이션 가능"
        if not feasible:
            for plan in group:
                proposed[id(plan)] = plan.daily_amount.amount
        summaries.append(
            RecurringBudgetSummary(
                currency=currency,
                current_weekly_amount=current_total,
                recommended_weekly_amount=requested_total if feasible else current_total,
                minimum_weekly_amount=minimum_total,
                maximum_weekly_amount=maximum_total,
                cap_applied=cap_blocked,
                floor_applied=floor_blocked,
                feasible=feasible,
                status=status or "현재 예산 유지 · 사용자 목표 미설정",
            )
        )

    recommendations: list[RecurringAmountRecommendation] = []
    for plan in plans_tuple:
        if not plan.enabled:
            recommendations.append(
                RecurringAmountRecommendation(
                    plan=plan,
                    recommended_amount=plan.daily_amount.amount,
                    action="비활성 유지",
                    reasons=("비활성 계획은 금액 조정 대상에서 제외",),
                )
            )
            continue
        amount = proposed[id(plan)]
        current = plan.daily_amount.amount
        action = "증액" if amount > current else ("감액" if amount < current else "유지")
        status = next(summary for summary in summaries if
                      summary.currency == plan.daily_amount.currency)
        reason = ("사용자 명시 금액안 · 자동 적용 없음" if action != "유지" else
                  "사용자 목표 미설정 · 현재 금액 유지" if requested_amounts is None else
                  status.status)
        recommendations.append(
            RecurringAmountRecommendation(
                plan=plan,
                recommended_amount=amount,
                action=action,
                reasons=(reason,),
            )
        )
    return tuple(recommendations), tuple(summaries)
