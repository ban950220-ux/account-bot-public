from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.domain import Currency, InstrumentKey, Money  # noqa: E402
from account_bot.portfolio import (  # noqa: E402
    RecurringAllocationSignal,
    RecurringBuyPlan,
    recommend_recurring_amounts,
)


class RecurringAllocationTests(unittest.TestCase):
    def test_valuation_does_not_change_amount_without_user_target(self) -> None:
        plans = tuple(
            RecurringBuyPlan(
                InstrumentKey("US", f"T{i}", currency=Currency.USD),
                Money("10000", Currency.KRW),
                broker="kiwoom_accum",
            )
            for i in range(10)
        )
        signals = {
            (plan.instrument.stable_id, plan.broker): RecurringAllocationSignal(
                exposure=f"sector-{index}",
                suitability_score=Decimal("85"),
                valuation_score=Decimal("80"),
            )
            for index, plan in enumerate(plans)
        }

        recommendations, summaries = recommend_recurring_amounts(plans, signals)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].recommended_weekly_amount,
                         summaries[0].current_weekly_amount)
        self.assertFalse(summaries[0].cap_applied)
        self.assertTrue(all(item.action == "유지" for item in recommendations))
        self.assertTrue(all("목표 미설정" in " ".join(item.reasons)
                            for item in recommendations))

    def test_weak_valuation_does_not_reduce_budget_without_user_target(self) -> None:
        plans = tuple(
            RecurringBuyPlan(
                InstrumentKey("US", f"W{i}", currency=Currency.USD),
                Money("10000", Currency.KRW),
                broker="kiwoom_accum",
            )
            for i in range(10)
        )
        signals = {
            (plan.instrument.stable_id, plan.broker): RecurringAllocationSignal(
                exposure="concentrated",
                suitability_score=Decimal("20"),
                valuation_score=Decimal("20"),
                has_hard_warning=True,
            )
            for plan in plans
        }

        _recommendations, summaries = recommend_recurring_amounts(plans, signals)

        self.assertEqual(summaries[0].recommended_weekly_amount,
                         summaries[0].current_weekly_amount)
        self.assertFalse(summaries[0].floor_applied)

    def test_sector_concentration_is_comparison_not_automatic_reduction(self) -> None:
        plan = RecurringBuyPlan(
            InstrumentKey("US", "WEAK", currency=Currency.USD),
            Money("10000", Currency.KRW),
            broker="kiwoom_accum",
        )
        signals = {
            (plan.instrument.stable_id, plan.broker): RecurringAllocationSignal(
                exposure="반도체",
                suitability_score=Decimal("30"),
                valuation_score=Decimal("25"),
            )
        }

        recommendations, _summaries = recommend_recurring_amounts((plan,), signals)

        self.assertEqual(recommendations[0].action, "유지")
        self.assertEqual(recommendations[0].recommended_amount, Decimal("10000"))
        reason = " ".join(recommendations[0].reasons)
        self.assertIn("목표 미설정", reason)

    def test_explicit_request_below_broker_minimum_is_infeasible(self) -> None:
        def recommendation_for(broker: str) -> tuple[Decimal, bool]:
            plan = RecurringBuyPlan(
                InstrumentKey("US", broker.upper(), currency=Currency.USD),
                Money("1000" if broker == "meritz" else "2000", Currency.KRW),
                broker=broker,
            )
            recommendations, summaries = recommend_recurring_amounts(
                (plan,), {}, maximum_total_decrease_pct=Decimal("100"),
                requested_amounts={(plan.instrument.stable_id, plan.broker,
                                    plan.account_id): Decimal("0")},
                preserve_weekly_budget=False,
            )
            return recommendations[0].recommended_amount, summaries[0].feasible

        self.assertEqual(recommendation_for("kiwoom_accum"), (Decimal("2000"), False))
        self.assertEqual(recommendation_for("meritz"), (Decimal("1000"), False))

    def test_explicit_same_budget_reallocation_is_only_a_simulation(self) -> None:
        plans = tuple(RecurringBuyPlan(
            InstrumentKey("US", ticker, currency=Currency.USD),
            Money(amount, Currency.KRW), broker="kiwoom_accum",
        ) for ticker, amount in (("A", "2000"), ("B", "4000")))
        requests = {(plan.instrument.stable_id, plan.broker, plan.account_id): amount
                    for plan, amount in zip(plans, (Decimal("3000"), Decimal("3000")), strict=True)}
        recommendations, summaries = recommend_recurring_amounts(
            plans, {}, requested_amounts=requests)
        self.assertEqual([item.action for item in recommendations], ["증액", "감액"])
        self.assertTrue(summaries[0].feasible)
        self.assertEqual(summaries[0].current_weekly_amount,
                         summaries[0].recommended_weekly_amount)

    def test_explicit_budget_cap_rejects_unbalanced_request_without_rounding(self) -> None:
        plan = RecurringBuyPlan(InstrumentKey("US", "A", currency=Currency.USD),
                                Money("2000", Currency.KRW), broker="kiwoom_accum")
        key = (plan.instrument.stable_id, plan.broker, plan.account_id)
        recommendations, summaries = recommend_recurring_amounts(
            (plan,), {}, requested_amounts={key: Decimal("4000")},
            preserve_weekly_budget=False)
        self.assertFalse(summaries[0].feasible)
        self.assertTrue(summaries[0].cap_applied)
        self.assertEqual(recommendations[0].recommended_amount, Decimal("2000"))

    def test_floor_and_step_fail_closed_with_current_amount_unchanged(self) -> None:
        plan = RecurringBuyPlan(InstrumentKey("US", "A", currency=Currency.USD),
                                Money("10000", Currency.KRW), broker="kiwoom_accum")
        key = (plan.instrument.stable_id, plan.broker, plan.account_id)
        floor, floor_summary = recommend_recurring_amounts(
            (plan,), {}, requested_amounts={key: Decimal("8000")},
            preserve_weekly_budget=False)
        self.assertEqual(floor[0].recommended_amount, Decimal("10000"))
        self.assertTrue(floor_summary[0].floor_applied)
        self.assertFalse(floor_summary[0].feasible)
        step, step_summary = recommend_recurring_amounts(
            (plan,), {}, requested_amounts={key: Decimal("9500")})
        self.assertEqual(step[0].recommended_amount, Decimal("10000"))
        self.assertIn("단위", step_summary[0].status)
        invalid, invalid_summary = recommend_recurring_amounts(
            (plan,), {}, requested_amounts={key: "not-a-number"})
        self.assertEqual(invalid[0].recommended_amount, Decimal("10000"))
        self.assertIn("숫자 형식", invalid_summary[0].status)


if __name__ == "__main__":
    unittest.main()
