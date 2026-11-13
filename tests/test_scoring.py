from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.analysis import (  # noqa: E402
    CompanyScoreInput,
    Decision,
    HardWarning,
    PortfolioScoreInput,
    ScoreResult,
    buy_capacity_score,
    calculate_company_score,
    calculate_final_score,
    calculate_portfolio_score,
    calculate_valuation_decision,
)
from account_bot.domain import BrokerResult  # noqa: E402
from account_bot.market import TrendData  # noqa: E402
from account_bot.portfolio import PortfolioAggregator  # noqa: E402
from account_bot.services import CompanyAnalysisService  # noqa: E402
from account_bot.services.analysis_service import recurring_suitability  # noqa: E402


class NoDataProvider:
    async def get_price(self, market: str, ticker: str):
        raise RuntimeError("fixture unavailable")

    async def get_fx(self, base: str, quote: str):
        raise RuntimeError("fixture unavailable")

    async def get_fundamentals(self, market: str, ticker: str):
        raise RuntimeError("fixture unavailable")

    async def get_valuation(self, market: str, ticker: str):
        raise RuntimeError("fixture unavailable")

    async def get_earnings(self, market: str, ticker: str):
        raise RuntimeError("fixture unavailable")

    async def get_news(self, market: str, ticker: str, *, limit: int = 10):
        raise RuntimeError("fixture unavailable")


class ScoringTests(unittest.TestCase):
    def test_recurring_suitability_rewards_durable_trend_and_bounded_risk(self) -> None:
        strong = recurring_suitability(
            TrendData(
                market="US",
                ticker="ETF",
                as_of=datetime(2026, 8, 11, tzinfo=timezone.utc),
                provider="fixture",
                observation_count=1000,
                return_1y=Decimal("0.12"),
                cagr_3y=Decimal("0.11"),
                max_drawdown_3y=Decimal("-0.20"),
                annualized_volatility_1y=Decimal("0.18"),
                above_200d_average=True,
            )
        )
        weak = recurring_suitability(
            TrendData(
                market="US",
                ticker="ETF",
                as_of=datetime(2026, 8, 11, tzinfo=timezone.utc),
                provider="fixture",
                observation_count=1000,
                return_1y=Decimal("-0.20"),
                cagr_3y=Decimal("-0.05"),
                max_drawdown_3y=Decimal("-0.60"),
                annualized_volatility_1y=Decimal("0.50"),
                above_200d_average=False,
            )
        )

        self.assertIsNotNone(strong.score)
        self.assertIsNotNone(weak.score)
        self.assertGreater(strong.score, Decimal("70"))
        self.assertLess(weak.score, Decimal("40"))

    def test_company_score_is_deterministic(self) -> None:
        result = calculate_company_score(CompanyScoreInput("80", "70", "90", "60", "50"))
        self.assertEqual(result.score, Decimal("74.50"))
        self.assertEqual(result.coverage_pct, Decimal("100.00"))
        self.assertTrue(result.is_eligible)

    def test_coverage_gate_withholds_sparse_company_score(self) -> None:
        result = calculate_company_score(CompanyScoreInput("80", "70", None, None, None))
        self.assertEqual(result.coverage_pct, Decimal("60.00"))
        self.assertIsNone(result.score)
        self.assertFalse(result.is_eligible)

    def test_available_components_are_renormalized_only_above_gate(self) -> None:
        result = calculate_portfolio_score(PortfolioScoreInput(True, "80", "70", "60", "50", None))
        self.assertEqual(result.coverage_pct, Decimal("95.00"))
        self.assertEqual(
            result.score,
            (Decimal("80") * 50 + Decimal("70") * 20 + Decimal("60") * 15 + Decimal("50") * 10)
            .__truediv__(Decimal("95"))
            .quantize(Decimal("0.01")),
        )
        self.assertIn("score_renormalized_over_available_components", result.warnings)

    def test_partial_portfolio_always_withholds_portfolio_score(self) -> None:
        result = calculate_portfolio_score(
            PortfolioScoreInput(False, "100", "100", "100", "100", "100")
        )
        self.assertEqual(result.coverage_pct, Decimal("100.00"))
        self.assertIsNone(result.score)
        self.assertFalse(result.is_eligible)
        self.assertIn("score_withheld:portfolio_snapshot_incomplete", result.warnings)

    @staticmethod
    def available_score(value: str) -> ScoreResult:
        return ScoreResult(Decimal(value), Decimal("100"), True, ())

    def test_final_decision_boundaries(self) -> None:
        expected = {
            "85": Decision.AGGRESSIVE_BUY,
            "84.99": Decision.CONTINUE_BUY,
            "70": Decision.CONTINUE_BUY,
            "69.99": Decision.MAINTAIN,
            "55": Decision.MAINTAIN,
            "54.99": Decision.REDUCE,
            "40": Decision.REDUCE,
            "39.99": Decision.REVIEW_STOP,
        }
        for score, decision in expected.items():
            with self.subTest(score=score):
                result = calculate_final_score(
                    self.available_score(score), self.available_score(score)
                )
                self.assertEqual(result.score, Decimal(score).quantize(Decimal("0.01")))
                self.assertEqual(result.decision, decision)

    def test_hard_warning_overrides_a_high_score_but_preserves_audit_score(self) -> None:
        result = calculate_final_score(
            self.available_score("95"),
            self.available_score("95"),
            hard_warnings=(HardWarning.FRAUD,),
        )
        self.assertEqual(result.score, Decimal("95.00"))
        self.assertEqual(result.decision, Decision.REVIEW_REQUIRED)
        self.assertFalse(result.is_actionable)
        self.assertEqual(result.hard_warnings, (HardWarning.FRAUD,))

    def test_unavailable_input_with_hard_warning_still_requires_review(self) -> None:
        unavailable = ScoreResult(None, Decimal("50"), False, ())
        result = calculate_final_score(
            unavailable,
            self.available_score("90"),
            hard_warnings=(HardWarning.GOING_CONCERN,),
        )
        self.assertIsNone(result.score)
        self.assertEqual(result.decision, Decision.REVIEW_REQUIRED)
        self.assertFalse(result.is_actionable)

    def test_buy_capacity_penalizes_overweight_positions(self) -> None:
        self.assertEqual(buy_capacity_score("6", "8"), Decimal("100"))
        self.assertEqual(buy_capacity_score("12", "8"), Decimal("50.00"))
        self.assertEqual(buy_capacity_score("16", "8"), Decimal("0.00"))
        self.assertEqual(buy_capacity_score("1", "0"), Decimal("0"))

    def test_valuation_decision_does_not_require_portfolio_score(self) -> None:
        result = calculate_valuation_decision(Decimal("72"))

        self.assertEqual(result.score, Decimal("72.00"))
        self.assertEqual(result.decision, Decision.CONTINUE_BUY)
        self.assertTrue(result.is_actionable)
        self.assertIsNone(result.portfolio_score)

    def test_valuation_decision_withholds_only_when_valuation_is_missing(self) -> None:
        result = calculate_valuation_decision(None)

        self.assertEqual(result.decision, Decision.UNAVAILABLE)
        self.assertFalse(result.is_actionable)


class AnalysisReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_sparse_data_still_explains_three_specific_withholding_reasons(self) -> None:
        snapshot = PortfolioAggregator().aggregate(
            (BrokerResult.ok("kiwoom", items=()),),
            expected_brokers=("kiwoom",),
            cash_results=(BrokerResult.ok("kiwoom", items=()),),
        )
        service = CompanyAnalysisService(NoDataProvider())

        result = await service.analyze(
            market="NASDAQ",
            ticker="AMD",
            portfolio=snapshot,
            target_weight_pct=Decimal("8"),
        )

        self.assertEqual(len(result.reasons), 3)
        self.assertIn("정량 데이터", result.reasons[0])
        self.assertIn("커버리지", result.reasons[1])
        self.assertIn("포트폴리오 점수", result.reasons[2])


if __name__ == "__main__":
    unittest.main()
