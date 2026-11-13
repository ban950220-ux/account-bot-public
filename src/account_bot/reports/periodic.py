"""Concise daily and deeper weekly report composition."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from html import escape

from account_bot.analysis import TargetAllocation, suggest_new_money_rebalance
from account_bot.domain import Currency, Money, instruments_compatible
from account_bot.portfolio import RecurringBuyPlan
from account_bot.services.analysis_service import AnalysisResult
from account_bot.services.portfolio_service import PortfolioRefresh

from .formatter import format_assets, format_money, format_portfolio, format_recurring


def build_periodic_report(
    report_type: str,
    refresh: PortfolioRefresh,
    *,
    recurring_plans: Iterable[RecurringBuyPlan] = (),
    analyses: Mapping[str, AnalysisResult] | None = None,
) -> str:
    """Shared pure report entry point for polling and a future Cron adapter."""
    if report_type == "daily":
        return build_daily_report(refresh, recurring_plans=recurring_plans, analyses=analyses)
    if report_type == "weekly":
        return build_weekly_report(refresh, recurring_plans=recurring_plans, analyses=analyses)
    raise ValueError("unsupported periodic report type")


def build_daily_report(
    refresh: PortfolioRefresh,
    *,
    recurring_plans: Iterable[RecurringBuyPlan] = (),
    analyses: Mapping[str, AnalysisResult] | None = None,
) -> str:
    plans = tuple(recurring_plans)
    lines = ["📊 <b>Daily Portfolio Report</b>", "", format_assets(refresh)]
    lines += [
        "",
        "━━━━━━━━━━",
        "",
        format_recurring(plans, refresh.snapshot, analyses),
    ]
    lines += [
        "",
        "━━━━━━━━━━",
        "",
        "⚠️ <b>주요 변화</b>",
        "",
        "실적·뉴스 변화는 확인된 데이터만 표시하며, 단기 가격 움직임은 Thesis 변화로 간주하지 않습니다.",
    ]
    if not refresh.snapshot.is_complete:
        lines += ["", "오늘은 계좌 데이터가 불완전해 Portfolio 판단을 보류합니다."]
    else:
        lines += ["", "특별 대응 여부는 위 적립 종목별 근거를 확인하세요."]
    return "\n".join(lines)


def build_weekly_report(
    refresh: PortfolioRefresh,
    *,
    recurring_plans: Iterable[RecurringBuyPlan] = (),
    analyses: Mapping[str, AnalysisResult] | None = None,
    next_week_budget_krw: Decimal | None = None,
) -> str:
    lines = [
        "📅 <b>Weekly Portfolio Report</b>",
        "",
        format_assets(refresh),
        "",
        "━━━━━━━━━━",
        "",
        format_portfolio(refresh.snapshot),
    ]
    lines += [
        "",
        "━━━━━━━━━━",
        "",
        "📈 <b>성과</b>",
        "1W/YTD 수익률은 완전한 과거 snapshot과 입출금 조정 이력이 쌓인 뒤 제공합니다.",
    ]
    lines += ["", "━━━━━━━━━━", "", "🧭 <b>Thesis·Valuation</b>"]
    plans = tuple(recurring_plans)
    if not plans:
        lines += ["", "등록된 적립 종목 없음"]
    for plan in plans:
        analysis = (analyses or {}).get(plan.instrument.stable_id)
        if analysis:
            valuation_score = next(
                (
                    component.score
                    for component in analysis.company_score.components
                    if component.name == "valuation"
                ),
                None,
            )
            decision_labels = {
                "aggressive_buy": "적극 증액",
                "continue_buy": "증액",
                "maintain": "유지",
                "reduce": "감액",
                "review_stop": "중단 검토",
                "review_required": "중요 경고 재검토",
                "unavailable": "Valuation 데이터 부족으로 판단 보류",
            }
            lines += [
                "",
                f"<b>{escape(plan.instrument.display_id)}</b>",
                f"주식봇 Valuation {valuation_score if valuation_score is not None else '보류'}",
                "결론 "
                + decision_labels.get(
                    analysis.final_score.decision.value,
                    analysis.final_score.decision.value,
                ),
                "장기추세 적합성 "
                + (
                    f"{analysis.recurring_suitability.score}"
                    if analysis.recurring_suitability.score is not None
                    else "보류"
                ),
            ]
            if plan.investment_thesis:
                lines.append("Thesis: " + escape(plan.investment_thesis[:240]))
            if analysis.valuation and analysis.valuation.forward_pe is not None:
                valuation = (
                    f"금요일 종가 기준 Forward PER {analysis.valuation.forward_pe:.2f}x"
                )
                if analysis.valuation.historical_forward_pe_median is not None:
                    valuation += (
                        f" · 과거 median {analysis.valuation.historical_forward_pe_median:.2f}x"
                    )
                else:
                    valuation += " · 과거 median 미제공"
                lines.append(valuation)
            if analysis.earnings:
                earnings = analysis.earnings
                summary = f"최근 실적 {escape(earnings.period or '확인 분기')}"
                if earnings.revenue_yoy is not None:
                    summary += f" · Revenue YoY {earnings.revenue_yoy * 100:+.2f}%"
                lines.append(summary)
            review_news = [
                item for item in analysis.news if item.fundamental_impact == "REVIEW_REQUIRED"
            ]
            if review_news:
                lines.append(
                    f"Fundamental 영향 원문 확인 필요 뉴스 {len(review_news)}건: "
                    + escape(review_news[0].title[:160])
                )
            safety = analysis.company_score.components[4].score
            lines.append(f"Safety {safety if safety is not None else '보류'}")
        else:
            lines += [
                "",
                f"<b>{escape(plan.instrument.display_id)}</b>",
                "분석 데이터 부족",
            ]
    lines += ["", "━━━━━━━━━━", "", "⚖️ <b>다음 주 배분</b>"]
    if next_week_budget_krw is None and plans:
        computed_budget = Decimal("0")
        budget_available = True
        for plan in plans:
            if not plan.enabled:
                continue
            weekly_amount = plan.daily_amount.amount * Decimal(len(plan.scheduled_weekdays))
            if plan.daily_amount.currency == Currency.KRW:
                computed_budget += weekly_amount
            elif plan.daily_amount.currency == Currency.USD and refresh.fx_quote is not None:
                computed_budget += weekly_amount * refresh.fx_quote.rate
            else:
                budget_available = False
        if budget_available:
            next_week_budget_krw = computed_budget
    if next_week_budget_krw is None:
        lines.append("예상 신규 투자금이 설정되지 않아 배분안은 보류합니다.")
    elif not refresh.snapshot.is_complete:
        lines.append("계좌 데이터가 불완전해 신규 자금 배분안을 계산하지 않습니다.")
    else:
        lines.append(f"예상 신규 투자금 {next_week_budget_krw:,.0f}원")
        targets = []
        for recurring_plan in plans:
            if recurring_plan.target_weight_pct is None:
                continue
            matches = [
                position.instrument
                for position in refresh.snapshot.positions
                if instruments_compatible(position.instrument, recurring_plan.instrument)
            ]
            instrument = matches[0] if len(matches) == 1 else recurring_plan.instrument
            targets.append(TargetAllocation(instrument, recurring_plan.target_weight_pct))
        try:
            rebalance = suggest_new_money_rebalance(
                refresh.snapshot,
                targets,
                Money(next_week_budget_krw, Currency.KRW),
            )
        except ValueError:
            lines.append("목표 비중 합계가 올바르지 않아 배분 계산을 보류합니다.")
        else:
            for item in rebalance.items:
                if item.suggested_new_money.amount > 0:
                    lines.append(
                        f"{escape(item.instrument.display_id)} → "
                        f"{format_money(item.suggested_new_money.amount, Currency.KRW)}"
                    )
            if rebalance.unallocated_cash.amount > 0:
                lines.append(
                    "현금 유지 → " + format_money(rebalance.unallocated_cash.amount, Currency.KRW)
                )
    lines += ["", "리밸런싱은 매도 대신 신규 자금 배분 변경을 우선합니다."]
    return "\n".join(lines)
