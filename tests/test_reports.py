from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.domain import (  # noqa: E402
    AssetType,
    BrokerError,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    InstrumentKey,
    Money,
    Position,
)
from account_bot.portfolio import PortfolioAggregator, RecurringBuyPlan  # noqa: E402
from account_bot.reports import (  # noqa: E402
    build_daily_report,
    build_periodic_report,
    build_weekly_report,
    format_assets,
    format_portfolio,
    format_recurring,
)
from account_bot.services import PortfolioRefresh  # noqa: E402


class ReportFormatterTests(unittest.TestCase):
    def test_weekly_budget_uses_each_enabled_plans_scheduled_days(self) -> None:
        result = BrokerResult.ok(
            "kiwoom",
            (
                Position(
                    "kiwoom",
                    "safe",
                    InstrumentKey("KRX", "005930", currency=Currency.KRW),
                    "1",
                    market_value="100000",
                ),
            ),
        )
        refresh = PortfolioRefresh(
            snapshot=PortfolioAggregator(Currency.KRW).aggregate((result,)),
            account_results=(),
            position_results=(result,),
            cash_results=(),
            fx_quote=None,
            collected_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        )
        plans = (
            RecurringBuyPlan(
                InstrumentKey("US", "MON", currency=Currency.USD),
                Money("2000", Currency.KRW),
                scheduled_weekdays=(0,),
            ),
            RecurringBuyPlan(
                InstrumentKey("US", "DAILY", currency=Currency.USD),
                Money("1000", Currency.KRW),
            ),
            RecurringBuyPlan(
                InstrumentKey("US", "OFF", currency=Currency.USD),
                Money("3000", Currency.KRW),
                enabled=False,
            ),
        )

        rendered = build_weekly_report(refresh, recurring_plans=plans)
        self.assertEqual(
            build_periodic_report("weekly", refresh, recurring_plans=plans), rendered
        )
        self.assertEqual(
            build_periodic_report("daily", refresh, recurring_plans=plans),
            build_daily_report(refresh, recurring_plans=plans),
        )

        self.assertIn("예상 신규 투자금 7,000원", rendered)
        monday_only = build_weekly_report(refresh, recurring_plans=plans[:1])
        self.assertIn("예상 신규 투자금 2,000원", monday_only)
        self.assertNotIn("예상 신규 투자금 10,000원", monday_only)

    def test_recurring_report_shows_basket_weight_and_assumption(self) -> None:
        plans = (
            RecurringBuyPlan(
                InstrumentKey("US", "DGRO", AssetType.ETF, Currency.USD),
                Money(Decimal("30000"), Currency.KRW),
                broker="meritz",
            ),
            RecurringBuyPlan(
                InstrumentKey("US", "VGT", AssetType.ETF, Currency.USD),
                Money(Decimal("10000"), Currency.KRW),
                broker="meritz",
            ),
        )
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(())

        rendered = format_recurring(plans, snapshot)

        self.assertIn("평일 매일 30,000원 · 비중 75.00%", rendered)
        self.assertIn("📂 <b>ETF</b>", rendered)
        self.assertIn("활성 2종목 · 균등 기준 50.00%", rendered)
        self.assertIn("최종 요약", rendered)
        self.assertGreater(rendered.index("최종 요약"), rendered.index("US:VGT"))
        self.assertIn("주간 환산", rendered)
        self.assertNotIn("현재 비중", rendered)
        self.assertNotIn("목표 비중 75", rendered)
        self.assertNotIn("수익률 확인 불가", rendered)
        # The compact report shows a plan's source, key and unchanged-amount
        # placeholders nowhere; comparison lines appear only for a real change.
        self.assertIn("<b>US:DGRO</b> · 옵션인컴·배당 ETF", rendered)
        self.assertIn("비중은 신규 모으기 투입액 기준", rendered)
        self.assertIn("ETF는 한 노출군으로만 분류", rendered)
        self.assertIn("종목 분석 데이터가 없어 점수와 금액 추천은 표시하지 않습니다", rendered)
        for hidden in ("출처", "계획키", "1회 금액 비교", "사용자 목표 미설정",
                       "시뮬레이션", "조정 종목 요약"):
            self.assertNotIn(hidden, rendered)
        self.assertNotIn("주간 금액 비중 75.0%로 쏠림", rendered)
        self.assertNotIn("+5% 상한", rendered)
        self.assertLess(rendered.index("US:DGRO"), rendered.index("US:VGT"))

    def test_recurring_budget_keeps_weekday_and_currency_denominators_separate(self) -> None:
        plans = (
            RecurringBuyPlan(
                InstrumentKey("US", "MON", AssetType.EQUITY, Currency.USD),
                Money("2000", Currency.KRW), scheduled_weekdays=(0,)),
            RecurringBuyPlan(
                InstrumentKey("US", "FIVE", AssetType.EQUITY, Currency.USD),
                Money("1000", Currency.KRW), scheduled_weekdays=(0, 1, 2, 3, 4)),
            RecurringBuyPlan(
                InstrumentKey("US", "DOLLAR", AssetType.ETF, Currency.USD),
                Money("10", Currency.USD), scheduled_weekdays=(4,)),
        )
        rendered = format_recurring(plans, PortfolioAggregator(Currency.KRW).aggregate(()))
        self.assertIn("월 3,000원", rendered)
        self.assertIn("화 1,000원", rendered)
        self.assertIn("주간 예정 투입 7,000원", rendered)
        self.assertIn("주간 예정 투입 $10.00", rendered)
        self.assertIn("월평균 환산 30,333원", rendered)
        self.assertIn("월평균 환산 $43.33", rendered)
        self.assertIn("통화별로 따로 계산합니다", rendered)
        self.assertIn("분류 확인 불가", rendered)

    def test_recurring_report_hides_holdings_and_fill_history(self) -> None:
        instrument = InstrumentKey("US", "NVTS", currency=Currency.USD)
        position_result = BrokerResult.ok(
            "kiwoom_accum",
            (
                Position(
                    "kiwoom_accum",
                    "safe-alias",
                    instrument,
                    quantity="1",
                    average_price="5",
                    current_price="6",
                ),
            ),
        )
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (position_result,), expected_brokers=("kiwoom_accum",)
        )

        rendered = format_recurring((), snapshot)

        self.assertNotIn("소수점 체결 기반 추정", rendered)
        self.assertNotIn("NASDAQ:TEST", rendered)
        self.assertNotIn("$2.01", rendered)
        self.assertNotIn("US:NVTS", rendered)
        self.assertNotIn("보유수량 1주", rendered)

    def test_assets_formats_zero_weight_region_as_decimal(self) -> None:
        instrument = InstrumentKey("KRX", "005930", currency=Currency.KRW)
        position_result = BrokerResult.ok(
            "kiwoom",
            (Position("kiwoom", "safe", instrument, "1", market_value="100"),),
        )
        cash_result = BrokerResult.ok(
            "kiwoom",
            (CashBalance("kiwoom", "safe", Currency.KRW, "10"),),
        )
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(
            (position_result,),
            expected_brokers=("kiwoom",),
            cash_results=(cash_result,),
        )
        refresh = PortfolioRefresh(
            snapshot=snapshot,
            account_results=(),
            position_results=(position_result,),
            cash_results=(cash_result,),
            fx_quote=None,
            collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        )

        rendered = format_assets(refresh)

        self.assertIn("미국주식 0.00%", rendered)
        self.assertIn("한국주식", rendered)

    def test_partial_portfolio_explicitly_says_weights_are_withheld_not_zero(self) -> None:
        instrument = InstrumentKey("KRX", "005930", currency=Currency.KRW)
        known = Position(
            "kiwoom",
            "safe-account",
            instrument,
            quantity="10",
            cost_basis="1000000",
            market_value="1200000",
            company_name="삼성전자",
        )
        unavailable = BrokerResult.failure(
            "toss",
            BrokerStatus.UNAVAILABLE,
            error=BrokerError("TIMEOUT", "증권사 조회에 실패했습니다.", retryable=True),
        )
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(
            (BrokerResult.ok("kiwoom", (known,)), unavailable),
            expected_brokers=("kiwoom", "toss"),
        )

        rendered = format_portfolio(snapshot)

        self.assertIn("키움 메인 포트폴리오 부분 조회", rendered)
        self.assertIn("누락 증권사를 0원으로 두지 않기 위해 보류", rendered)
        self.assertIn("비중 확인 불가", rendered)
        self.assertNotIn("비중 0.00%", rendered)

if __name__ == "__main__":
    unittest.main()
