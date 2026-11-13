"""Escaped HTML renderers kept below Telegram's message-size limit."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from account_bot.analysis import RebalanceAction, RebalancePlan
from account_bot.domain import AssetType, BrokerStatus, Currency
from account_bot.portfolio import (
    PortfolioSnapshot,
    RecurringBuyPlan,
    recommend_recurring_amounts,
)
from account_bot.services.analysis_service import AnalysisResult
from account_bot.services.portfolio_service import PortfolioRefresh

_KST = ZoneInfo("Asia/Seoul")
_DIVIDER = "━━━━━━━━━━"

_RECURRING_EXPOSURE: dict[str, str] = {
    "QQQ": "미국 대형 기술·성장 ETF",
    "QLD": "미국 대형 기술·성장 ETF",
    "MAGS": "미국 대형 기술·성장 ETF",
    "VGT": "미국 대형 기술·성장 ETF",
    "VOO": "미국 광범위 지수",
    "SSO": "미국 광범위 지수",
    "SPMO": "미국 광범위 지수",
    "IWM": "미국 광범위 지수",
    "VTWO": "미국 광범위 지수",
    "VUG": "미국 광범위 지수",
    "USD": "반도체",
    "SOXQ": "반도체",
    "SOXX": "반도체",
    "ASX": "반도체",
    "TSM": "반도체",
    "KLAC": "반도체",
    "LRCX": "반도체",
    "ASML": "반도체",
    "MPWR": "반도체",
    "STM": "반도체",
    "MRVL": "반도체",
    "NVDA": "반도체",
    "AMD": "반도체",
    "AVGO": "반도체",
    "MU": "반도체",
    "ALGM": "반도체",
    "TXN": "반도체",
    "ARM": "반도체",
    "QCOM": "반도체",
    "XLF": "금융",
    "CME": "금융",
    "AMZN": "경기소비재",
    "TSLA": "경기소비재",
    "PAVE": "산업재",
    "CAT": "산업재",
    "RRX": "산업재",
    "CGNX": "산업재",
    "JCI": "산업재",
    "TKR": "산업재",
    "XYL": "산업재",
    "TT": "산업재",
    "ETN": "산업재",
    "ROK": "산업재",
    "NOVT": "산업재·기술",
    "STX": "데이터저장·기술",
    "WDC": "데이터저장·기술",
    "SNDK": "데이터저장·기술",
    "COHR": "광통신·기술",
    "LITE": "광통신·기술",
    "GLW": "광통신·기술",
    "GOOG": "커뮤니케이션",
    "QNT": "양자컴퓨팅·기술",
    "BE": "청정에너지",
    "FLNC": "청정에너지",
    "ECL": "소재",
    "XLV": "헬스케어",
    "LLY": "헬스케어",
    "UNH": "헬스케어",
    "PLTR": "소프트웨어",
    "GEV": "전력인프라",
    "VRT": "전력인프라",
    "QDVO": "옵션인컴·배당 ETF",
    "QDTE": "옵션인컴·배당 ETF",
    "QYLD": "옵션인컴·배당 ETF",
    "QQQI": "옵션인컴·배당 ETF",
    "GPIQ": "옵션인컴·배당 ETF",
    "JEPQ": "옵션인컴·배당 ETF",
    "SCHD": "옵션인컴·배당 ETF",
    "DIVO": "옵션인컴·배당 ETF",
    "VYM": "옵션인컴·배당 ETF",
    "DGRO": "옵션인컴·배당 ETF",
    "GLDM": "금",
}
_US_RECURRING_MARKETS = frozenset({"US", "NASDAQ", "NYSE", "AMEX"})
_RECURRING_ETFS = frozenset({
    "QQQ", "QLD", "MAGS", "VGT", "VOO", "SSO", "SPMO", "IWM", "VTWO", "VUG",
    "USD", "SOXQ", "SOXX", "XLF", "PAVE", "XLV", "QDVO", "QDTE", "QYLD",
    "QQQI", "GPIQ", "JEPQ", "SCHD", "DIVO", "VYM", "DGRO", "GLDM",
})


def _recurring_schedule_text(plan: RecurringBuyPlan) -> str:
    if not plan.scheduled_weekdays:
        return "예정 요일 없음"
    if plan.scheduled_weekdays == (0, 1, 2, 3, 4):
        return "평일 매일"
    if plan.scheduled_weekdays == (0,):
        return "매주 월요일"
    if plan.scheduled_weekdays == (4,):
        return "매주 금요일"
    names = ("월", "화", "수", "목", "금", "토", "일")
    return "매주 " + "·".join(names[day] for day in plan.scheduled_weekdays)


def _weekly_plan_amount(plan: RecurringBuyPlan) -> Decimal:
    return plan.daily_amount.amount * Decimal(len(plan.scheduled_weekdays))


def _recurring_exposure(plan: RecurringBuyPlan, analysis: AnalysisResult | None) -> str:
    ticker = plan.instrument.ticker
    mapped = _RECURRING_EXPOSURE.get(ticker)
    expected_type = AssetType.ETF if ticker in _RECURRING_ETFS else AssetType.EQUITY
    if (mapped and plan.instrument.market in _US_RECURRING_MARKETS and
            plan.instrument.asset_type == expected_type):
        return mapped
    # A company sector is not an ETF constituent look-through.
    if (plan.instrument.asset_type != AssetType.ETF and analysis and
            analysis.fundamentals and analysis.fundamentals.sector):
        return analysis.fundamentals.sector
    return "분류 확인 불가"


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def format_money(amount: Decimal, currency: Currency | str) -> str:
    code = currency.value if isinstance(currency, Currency) else str(currency).upper()
    if code == "KRW":
        return f"{_q(amount, '1'):,.0f}원"
    if code == "USD":
        return f"${_q(amount):,.2f}"
    return f"{_q(amount):,.2f} {escape(code)}"


def format_pct(value: Decimal | None) -> str:
    if value is None:
        return "확인 불가"
    sign = "+" if value > 0 else ""
    return f"{sign}{_q(value):,.2f}%"


def _score_text(value: Decimal | None) -> str:
    return "보류" if value is None else str(_q(value))


def _status_text(status: BrokerStatus) -> str:
    return {
        BrokerStatus.OK: "정상",
        BrokerStatus.PARTIAL: "일부만 조회",
        BrokerStatus.STALE: "마지막 정상값",
        BrokerStatus.NOT_CONFIGURED: "미설정",
        BrokerStatus.AUTH_FAILED: "인증 실패",
        BrokerStatus.RATE_LIMITED: "호출 제한",
        BrokerStatus.UNAVAILABLE: "조회 실패",
    }[status]


def _convert_to_krw(
    amount: Decimal, currency: Currency, refresh: PortfolioRefresh
) -> Decimal | None:
    if currency == Currency.KRW:
        return amount
    if currency == Currency.USD and refresh.fx_quote is not None:
        return amount * refresh.fx_quote.rate
    return None


def _broker_totals(refresh: PortfolioRefresh) -> dict[str, Decimal | None]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    valid: dict[str, bool] = defaultdict(lambda: True)
    for result in refresh.position_results:
        if result.status != BrokerStatus.OK:
            valid[result.broker] = False
        if result.status in {BrokerStatus.OK, BrokerStatus.PARTIAL, BrokerStatus.STALE}:
            for position in result.items:
                local = position.effective_market_value
                converted = (
                    _convert_to_krw(local, position.instrument.currency, refresh)
                    if local is not None
                    else None
                )
                if converted is None:
                    valid[result.broker] = False
                else:
                    totals[result.broker] += converted
    for result in refresh.cash_results:
        if result.status != BrokerStatus.OK:
            valid[result.broker] = False
        if result.status in {BrokerStatus.OK, BrokerStatus.PARTIAL, BrokerStatus.STALE}:
            for cash in result.items:
                converted = _convert_to_krw(cash.amount, cash.currency, refresh)
                if converted is None:
                    valid[result.broker] = False
                else:
                    totals[result.broker] += converted
    brokers = {result.broker for result in refresh.position_results + refresh.cash_results}
    return {broker: totals[broker] if valid[broker] else None for broker in brokers}


def _freshness(refresh: PortfolioRefresh) -> str:
    local = refresh.collected_at.astimezone(_KST)
    completeness = "전체 조회" if refresh.snapshot.is_complete else "불완전 조회"
    return f"기준 {local:%Y-%m-%d %H:%M} KST · {completeness}"


def format_assets(refresh: PortfolioRefresh) -> str:
    snapshot = refresh.snapshot
    total = snapshot.total_market_value or snapshot.included_market_value
    title = (
        "💰 <b>키움 메인 전체자산</b>"
        if snapshot.is_complete
        else "⚠️ <b>키움 메인 확인 자산 부분 합계</b>"
    )
    lines = [title, "", "평가금액", f"<b>{format_money(total.amount, total.currency)}</b>"]
    native_totals: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    for result in refresh.position_results:
        if result.status in {BrokerStatus.OK, BrokerStatus.PARTIAL, BrokerStatus.STALE}:
            for position in result.items:
                if position.effective_market_value is not None:
                    native_totals[position.instrument.currency] += position.effective_market_value
    for result in refresh.cash_results:
        if result.status in {BrokerStatus.OK, BrokerStatus.PARTIAL, BrokerStatus.STALE}:
            for cash in result.items:
                native_totals[cash.currency] += cash.amount
    if native_totals:
        lines += ["", "확인된 현지통화 합계"]
        for currency in (Currency.KRW, Currency.USD):
            if currency in native_totals:
                lines.append(format_money(native_totals[currency], currency))
    cost = snapshot.total_cost_basis if snapshot.is_complete else snapshot.included_cost_basis
    if cost is not None:
        profit = total.amount - cost.amount
        return_pct = profit / cost.amount * Decimal("100") if cost.amount else None
        lines += [
            "",
            "투자원금",
            format_money(cost.amount, cost.currency),
            "",
            "평가손익",
            format_money(profit, total.currency),
            "",
            "수익률",
            format_pct(return_pct),
        ]
    else:
        lines += ["", "투자원금·수익률", "일부 원가 데이터가 없어 계산 보류"]

    lines += ["", _DIVIDER, "", "🏦 <b>증권사</b>"]
    broker_totals = _broker_totals(refresh)
    status_priority = {
        BrokerStatus.OK: 0,
        BrokerStatus.STALE: 1,
        BrokerStatus.PARTIAL: 2,
        BrokerStatus.NOT_CONFIGURED: 3,
        BrokerStatus.RATE_LIMITED: 4,
        BrokerStatus.AUTH_FAILED: 5,
        BrokerStatus.UNAVAILABLE: 6,
    }
    status_lists: dict[str, list[BrokerStatus]] = defaultdict(list)
    for broker, status in snapshot.position_statuses + snapshot.cash_statuses:
        status_lists[broker].append(status)
    statuses = {
        broker: max(values, key=lambda value: status_priority[value])
        for broker, values in status_lists.items()
    }
    for broker in snapshot.expected_brokers:
        label = {
            "kiwoom": "키움",
            "kiwoom_main": "키움 메인",
            "kiwoom_accum": "키움 모으기",
            "toss": "토스",
            "meritz": "메리츠",
        }.get(broker, broker)
        value = broker_totals.get(broker)
        status = statuses.get(broker, BrokerStatus.UNAVAILABLE)
        lines += [
            "",
            escape(label),
            format_money(value, Currency.KRW)
            if value is not None
            else f"{_status_text(status)} — 총액에서 0원 처리하지 않음",
        ]

    if snapshot.weights_available:
        korea = sum(
            (
                position.weight_pct or Decimal("0")
                for position in snapshot.positions
                if position.instrument.market in {"KRX", "KOSPI", "KOSDAQ", "NXT"}
            ),
            Decimal("0"),
        )
        us = sum(
            (
                position.weight_pct or Decimal("0")
                for position in snapshot.positions
                if position.instrument.market not in {"KRX", "KOSPI", "KOSDAQ", "NXT"}
            ),
            Decimal("0"),
        )
        lines += [
            "",
            _DIVIDER,
            "",
            "🌎 <b>지역</b>",
            "",
            f"미국주식 {format_pct(us)}",
            f"한국주식 {format_pct(korea)}",
            f"현금 {format_pct(snapshot.cash_weight_pct)}",
        ]
    else:
        lines += ["", _DIVIDER, "", "🌎 지역 비중", "불완전한 분모이므로 계산 보류"]

    if refresh.fx_quote is not None:
        stale = " · 마지막 정상값" if refresh.fx_quote.is_stale else ""
        lines += ["", f"USD/KRW {refresh.fx_quote.rate:,.2f}{stale}"]
    lines += ["", escape(_freshness(refresh))]
    return "\n".join(lines)


def format_accounts(refresh: PortfolioRefresh) -> str:
    lines = ["🏦 <b>증권사별 계좌</b>"]
    for result in refresh.account_results:
        label = {
            "kiwoom": "키움",
            "kiwoom_main": "키움 메인",
            "kiwoom_accum": "키움 모으기",
            "toss": "토스",
            "meritz": "메리츠",
        }.get(result.broker, result.broker)
        lines += ["", f"<b>{escape(label)}</b> · {_status_text(result.status)}"]
        if result.items:
            for account in result.items:
                # account_id is intentionally not rendered, even if an adapter
                # were accidentally to place a raw account number there.
                lines.append(f"• {escape(account.display_name or '등록 계좌')}")
        else:
            lines.append("• 표시 가능한 계좌 없음")
    lines += ["", "전체 계좌번호는 저장·표시하지 않습니다.", escape(_freshness(refresh))]
    return "\n".join(lines)


def format_portfolio(snapshot: PortfolioSnapshot, *, limit: int = 30) -> str:
    title = (
        "📊 <b>키움 메인 포트폴리오</b>"
        if snapshot.is_complete
        else "⚠️ <b>키움 메인 포트폴리오 부분 조회</b>"
    )
    positions = sorted(
        snapshot.positions,
        key=lambda item: item.market_value_base or Decimal("-1"),
        reverse=True,
    )
    lines = [title]
    if not positions:
        lines += ["", "조회된 보유 종목이 없습니다."]
    for index, position in enumerate(positions[:limit], start=1):
        name = escape(position.company_name or position.instrument.ticker)
        lines += ["", f"<b>{index}. {name} ({escape(position.instrument.display_id)})</b>"]
        if position.market_value_base is not None:
            lines.append(
                f"평가액 {format_money(position.market_value_base, snapshot.base_currency)}"
            )
        else:
            lines.append("평가액 확인 불가")
        lines.append(f"비중 {format_pct(position.weight_pct)}")
        lines.append(f"수익률 {format_pct(position.return_pct)}")
        if len(position.broker_quantities) > 1:
            detail = " · ".join(
                f"{escape(item.broker)} {item.quantity.normalize()}주"
                for item in position.broker_quantities
            )
            lines.append(f"합산 {position.quantity.normalize()}주 ({detail})")
    if len(positions) > limit:
        lines += ["", f"외 {len(positions) - limit}개 종목"]
    if snapshot.weights_available and positions and positions[0].weight_pct is not None:
        top = positions[0]
        if top.weight_pct >= Decimal("15"):
            lines += [
                "",
                "⚠️ <b>집중도</b>",
                f"{escape(top.instrument.display_id)} 비중이 {format_pct(top.weight_pct)}입니다.",
            ]
    if not snapshot.is_complete:
        lines += ["", "비중·집중도는 누락 증권사를 0원으로 두지 않기 위해 보류됩니다."]
    lines += ["", "섹터·국가 분류는 시장데이터가 확인된 종목만 주간 분석에 반영됩니다."]
    return "\n".join(lines)


def format_recurring(
    plans: Iterable[RecurringBuyPlan],
    snapshot: PortfolioSnapshot,
    analyses: Mapping[str, AnalysisResult] | None = None,
) -> str:
    plans_tuple = tuple(plans)
    del snapshot  # Holdings value weights need separately verified coverage and FX.
    lines = ["🌱 <b>매일모으기</b>"]
    if not plans_tuple:
        lines += [
            "",
            "등록된 종목이 없습니다.",
            "<code>/recurring_add AMD 20USD 8%</code>로 로컬 계획을 등록할 수 있습니다.",
        ]
        return "\n".join(lines)
    totals_by_currency: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    counts_by_currency: dict[Currency, int] = defaultdict(int)
    daily_by_currency: dict[Currency, dict[int, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    for plan in plans_tuple:
        if plan.enabled:
            totals_by_currency[plan.daily_amount.currency] += _weekly_plan_amount(plan)
            counts_by_currency[plan.daily_amount.currency] += 1
            for weekday in plan.scheduled_weekdays:
                daily_by_currency[plan.daily_amount.currency][weekday] += plan.daily_amount.amount

    exposure_amounts: dict[Currency, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    source_amounts: dict[Currency, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    for plan in plans_tuple:
        if not plan.enabled:
            continue
        analysis = (analyses or {}).get(plan.instrument.stable_id)
        exposure_amounts[plan.daily_amount.currency][_recurring_exposure(plan, analysis)] += (
            _weekly_plan_amount(plan)
        )
        source_amounts[plan.daily_amount.currency][plan.broker or "other"] += _weekly_plan_amount(
            plan
        )
    display_order = {AssetType.ETF: 0, AssetType.EQUITY: 1}
    display_plans = sorted(
        plans_tuple,
        key=lambda item: (
            display_order.get(item.instrument.asset_type, 2),
            item.broker or "",
            item.instrument.ticker,
        ),
    )
    current_type: AssetType | None = None
    for plan in display_plans:
        if plan.instrument.asset_type != current_type:
            current_type = plan.instrument.asset_type
            label = {
                AssetType.ETF: "ETF",
                AssetType.EQUITY: "개별주",
            }.get(current_type, "기타")
            lines += ["", f"📂 <b>{label}</b>"]
    recommendations, budget_summaries = recommend_recurring_amounts(plans_tuple, {})
    recommendation_by_plan = {id(item.plan): item for item in recommendations}
    summary_lines: list[str] = []
    if exposure_amounts:
        summary_lines += ["", "📌 <b>최종 요약 · 주간 환산</b>"]
        source_labels = {
            "kiwoom_accum": "키움 모으기",
            "meritz": "메리츠 모으기",
            "candidate": "추가 후보 7종목",
            "other": "기타",
        }
        for currency in sorted(exposure_amounts, key=lambda item: item.value):
            total = totals_by_currency[currency]
            summary_lines += ["", f"<b>주간 예정 투입 {format_money(total, currency)}</b>"]
            daily_text = " · ".join(
                f"{('월', '화', '수', '목', '금', '토', '일')[day]} "
                f"{format_money(amount, currency)}"
                for day, amount in sorted(daily_by_currency[currency].items())
            )
            summary_lines.append(f"요일별 1회 합계 {daily_text or '예정 요일 없음'}")
            monthly = total * Decimal("52") / Decimal("12")
            summary_lines.append(f"월평균 환산 {format_money(monthly, currency)}")
            for source, amount in sorted(
                source_amounts[currency].items(), key=lambda item: item[1], reverse=True
            ):
                summary_lines.append(
                    f"• {escape(source_labels.get(source, source))} "
                    f"{format_money(amount, currency)}"
                )
            summary_lines.append("노출군별 예정 투입 비중 (같은 통화의 주간 합계 기준)")
            ranked = sorted(
                exposure_amounts[currency].items(), key=lambda item: item[1], reverse=True
            )
            for exposure, amount in ranked:
                pct = amount / total * Decimal("100") if total else None
                warning = " ⚠️" if pct is not None and pct >= Decimal("25") else ""
                label = f"{_q(pct, '0.1'):.1f}%" if pct is not None else "확인 불가"
                summary_lines.append(f"• {escape(exposure)} {label}{warning}")
            summary_lines.append("⚠️는 기존 참고 경고선 25% 이상이며 목표 비중이 아닙니다.")
        if budget_summaries:
            summary_lines += ["", "💡 <b>현재 예산 유지 시뮬레이션</b>"]
            for budget in budget_summaries:
                change_pct = (
                    (budget.recommended_weekly_amount / budget.current_weekly_amount - 1)
                    * Decimal("100")
                    if budget.current_weekly_amount > 0
                    else Decimal("0")
                )
                summary_lines.append(
                    f"• {budget.currency.value} 주간 "
                    f"{format_money(budget.current_weekly_amount, budget.currency)} → "
                    f"{format_money(budget.recommended_weekly_amount, budget.currency)} "
                    f"({change_pct:+.1f}%)"
                )
                summary_lines.append(f"  상태: {escape(budget.status)}")

    for plan in plans_tuple:
        analysis = (analyses or {}).get(plan.instrument.stable_id)
        lines += ["", f"<b>{escape(plan.instrument.display_id)}</b>"]
        if plan.broker:
            source_label = {
                "kiwoom_accum": "키움 모으기",
                "meritz": "메리츠 모으기",
                "candidate": "사용자 추가 후보 · 키움 설정 미확인",
            }.get(plan.broker, plan.broker)
            lines.append(f"출처 {escape(source_label)}")
        lines.append(
            f"{_recurring_schedule_text(plan)} "
            f"{format_money(plan.daily_amount.amount, plan.daily_amount.currency)}"
        )
        daily_total = totals_by_currency[plan.daily_amount.currency]
        basket_weight = (
            _weekly_plan_amount(plan) / daily_total * Decimal("100")
            if plan.enabled and daily_total > 0
            else None
        )
        equal_weight = (
            Decimal("100") / Decimal(counts_by_currency[plan.daily_amount.currency])
            if counts_by_currency[plan.daily_amount.currency] > 0
            else None
        )
        lines.append(
            "모으기 내 비중 "
            + (f"{basket_weight:.2f}%" if basket_weight is not None else "확인 불가")
            + (f" · 균등 기준 {equal_weight:.2f}%" if equal_weight is not None else "")
            + " · 주간 환산"
        )
        lines.append(f"노출군 {_recurring_exposure(plan, analysis)}")
        if analysis is not None:
            valuation_score = next(
                (
                    component.score
                    for component in analysis.company_score.components
                    if component.name == "valuation"
                ),
                None,
            )
            lines.append(f"기업점수 {_score_text(analysis.company_score.score)} / 100")
            lines.append(f"Valuation {_score_text(valuation_score)} / 100")
            suitability = analysis.recurring_suitability
            lines.append(f"장기추세 적합성 {_score_text(suitability.score)} / 100")
            if suitability.reasons:
                lines.append("추세 근거: " + escape(" · ".join(suitability.reasons)))
            lines.append("모으기 예산 판단: 사용자 목표 미설정 · 현재 금액 유지")
        else:
            lines.append("종목 적합성: 분석 데이터 부족")
            lines.append("모으기 예산 판단: 사용자 목표 미설정 · 현재 금액 유지")
        recommendation = recommendation_by_plan[id(plan)]
        lines.append(
            "1회 금액 비교: "
            f"{format_money(plan.daily_amount.amount, plan.daily_amount.currency)} → "
            f"{format_money(recommendation.recommended_amount, plan.daily_amount.currency)} "
            f"({recommendation.action})"
        )
        if recommendation.reasons:
            lines.append("조정 근거: " + escape(" · ".join(recommendation.reasons)))
        if plan.investment_thesis:
            lines.append(f"Thesis: {escape(plan.investment_thesis[:180])}")
    lines += summary_lines
    lines += [
        "",
        "메리츠는 오늘 계획대로 실행됐다고 가정한 시뮬레이션이며 실제 체결 확인이 아닙니다.",
        "요일별·주간은 설정된 요일마다 1회 실행을 가정하며 휴장일은 차감하지 않습니다.",
        "월평균은 주간 × 52 ÷ 12이며 실제 월별 거래일 합계가 아닙니다.",
        "서로 다른 통화는 환율 근거 없이 합산하지 않습니다.",
        "이 비중은 신규 모으기 투입액 기준입니다. 보유자산 평가액 비중은 별도 검증 전까지 표시하지 않습니다.",
        "ETF는 단일 수동 노출군으로만 분류하며 내부 구성·직접 보유 중복은 계산하지 않습니다.",
        "사용자 예산·섹터 목표가 없어 자동 금액 변경안이나 투자 권고를 만들지 않습니다.",
    ]
    reduced = tuple(item for item in recommendations if item.action == "감액")
    increased = tuple(item for item in recommendations if item.action == "증액")
    lines += ["", "🧾 <b>모으기 금액 조정 종목 요약</b>"]
    if not reduced and not increased:
        lines.append("현재 계산상 금액을 변경할 종목이 없습니다.")
    if reduced:
        lines += ["", "🔻 <b>줄여야 할 종목</b>"]
        for item in reduced:
            currency = item.plan.daily_amount.currency
            reduction_reasons = tuple(
                reason
                for reason in item.reasons
                if "유지" not in reason and "보류" not in reason
            ) or item.reasons
            lines.append(
                f"• <b>{escape(item.plan.instrument.display_id)}</b> "
                f"{format_money(item.plan.daily_amount.amount, currency)} → "
                f"{format_money(item.recommended_amount, currency)}"
            )
            lines.append("  이유: " + escape(" · ".join(reduction_reasons)))
    if increased:
        lines += ["", "🔺 <b>늘릴 수 있는 종목</b>"]
        for item in increased:
            currency = item.plan.daily_amount.currency
            lines.append(
                f"• <b>{escape(item.plan.instrument.display_id)}</b> "
                f"{format_money(item.plan.daily_amount.amount, currency)} → "
                f"{format_money(item.recommended_amount, currency)}"
            )
            lines.append("  근거: " + escape(" · ".join(item.reasons)))
    return "\n".join(lines)


def _decision_text(result: AnalysisResult) -> str:
    mapping = {
        "aggressive_buy": "🟢 적극 매수",
        "continue_buy": "🟢 계속 매수",
        "maintain": "🟡 현재 수준 유지",
        "reduce": "🟠 매수 축소",
        "review_stop": "🔴 매수 중단 검토",
        "review_required": "🔴 중요 경고 검토 필요",
        "unavailable": "⚪ 판단 보류",
    }
    return mapping[result.final_score.decision.value]


def format_analysis(result: AnalysisResult) -> str:
    lines = [f"🔍 <b>{escape(result.market)}:{escape(result.ticker)} 투자분석</b>"]
    if result.price:
        freshness = " · 마지막 정상값" if result.price.stale else ""
        lines += [
            "",
            "현재가",
            format_money(result.price.price, result.price.currency) + freshness,
        ]
    else:
        lines += ["", "현재가", "확인 불가"]
    lines += [
        "",
        _DIVIDER,
        "",
        "📈 <b>Fundamental</b>",
        f"{_score_text(result.company_score.components[0].score)} / 100",
    ]
    if result.fundamentals:
        f = result.fundamentals
        if f.revenue_growth is not None:
            lines.append(f"Revenue growth {format_pct(f.revenue_growth * 100)}")
        if f.operating_margin is not None:
            lines.append(f"Operating margin {format_pct(f.operating_margin * 100)}")
        if f.free_cash_flow is not None:
            lines.append(f"FCF {f.free_cash_flow:,.0f}")
    else:
        lines.append("정량 데이터 부족")
    lines += ["", "📑 <b>최근 실적</b>"]
    if result.earnings:
        earnings = result.earnings
        lines.append(escape(earnings.period or "최근 확인 분기"))
        if earnings.revenue is not None:
            lines.append(f"Revenue {earnings.revenue:,.0f} (provider 표시 단위)")
        if earnings.revenue_yoy is not None:
            lines.append(f"Revenue YoY {format_pct(earnings.revenue_yoy * 100)}")
        if earnings.eps is not None:
            lines.append(f"EPS {earnings.eps:,.2f}")
        if earnings.eps_yoy is not None:
            lines.append(f"EPS YoY {format_pct(earnings.eps_yoy * 100)}")
        if earnings.gross_margin is not None:
            lines.append(f"Gross margin {format_pct(earnings.gross_margin * 100)}")
        if earnings.operating_margin is not None:
            lines.append(f"Operating margin {format_pct(earnings.operating_margin * 100)}")
        if earnings.guidance:
            lines.append("Guidance " + escape(earnings.guidance[:240]))
    else:
        lines.append("확인 가능한 분기 실적 데이터 없음")
    lines += [
        "",
        "💰 <b>Valuation</b>",
        f"{_score_text(result.company_score.components[1].score)} / 100",
    ]
    if result.valuation:
        v = result.valuation
        if v.close_price is not None:
            lines.append(
                f"주간 기준종가 {v.close_price:,.2f} · {v.as_of.astimezone(_KST):%Y-%m-%d}"
            )
        if v.forward_pe is not None:
            lines.append(f"Forward PER {v.forward_pe:.2f}x")
        if v.historical_forward_pe_median is not None:
            lines.append(f"과거 Median {v.historical_forward_pe_median:.2f}x")
        else:
            lines.append("과거 5Y median 미제공 — 절대 PER만으로 단정하지 않음")
        if v.peg is not None:
            lines.append(f"PEG {v.peg:.2f}")
        if v.enterprise_to_ebitda is not None:
            lines.append(f"EV/EBITDA {v.enterprise_to_ebitda:.2f}x")
        if v.free_cash_flow_yield is not None:
            lines.append(f"FCF Yield {v.free_cash_flow_yield * 100:.2f}%")
        if v.dilution_pct is not None:
            lines.append(f"주식수 변화 {v.dilution_pct:+.2f}%")
    else:
        lines.append("정량 데이터 부족")
    lines += [
        "",
        "🚀 <b>Growth</b>",
        f"{_score_text(result.company_score.components[2].score)} / 100",
        "",
        "🏰 <b>Moat</b>",
        "검증 가능한 경쟁우위 근거 미입력 시 점수 보류",
        "",
        "⚠️ <b>Safety</b>",
        f"{_score_text(result.company_score.components[4].score)} / 100",
        "",
        _DIVIDER,
        "",
        f"기업점수 {_score_text(result.company_score.score)} / 100",
        f"Portfolio 점수 {_score_text(result.portfolio_score.score)} / 100",
        f"최종 {_score_text(result.final_score.score)} / 100",
        f"장기추세 적합성 {_score_text(result.recurring_suitability.score)} / 100",
        "",
        f"<b>{_decision_text(result)}</b>",
        "",
        "이유",
    ]
    for index, reason in enumerate(result.reasons, start=1):
        lines.append(f"{index}. {escape(reason)}")
    review_news = [item for item in result.news if item.fundamental_impact == "REVIEW_REQUIRED"]
    if review_news:
        lines += ["", "📰 <b>Fundamental 영향 확인 필요 뉴스</b>"]
        for item in review_news[:3]:
            title = escape(item.title)
            if item.url.startswith("https://"):
                lines.append(f'• <a href="{escape(item.url, quote=True)}">{title}</a>')
            else:
                lines.append(f"• {title}")
        lines.append("뉴스 자체는 점수에 직접 반영하지 않았습니다.")
    if result.warnings:
        lines += ["", "데이터 경고: " + escape(", ".join(result.warnings))]
    return "\n".join(lines)


def format_rebalance(plan: RebalancePlan) -> str:
    lines = ["⚖️ <b>Portfolio Rebalancing</b>"]
    if not plan.is_eligible:
        return "\n".join(
            lines
            + [
                "",
                "불완전한 포트폴리오 분모이므로 제안 보류",
                "누락 계좌를 0원으로 간주하지 않습니다.",
            ]
        )
    for item in plan.items:
        if item.action == RebalanceAction.ADD_WITH_NEW_MONEY:
            action = f"신규 자금 {format_money(item.suggested_new_money.amount, item.suggested_new_money.currency)} 배분"
        elif item.action == RebalanceAction.OVERWEIGHT_NO_NEW_MONEY:
            action = "과다 — 신규 자금 배분 중단"
        else:
            action = "유지"
        lines += [
            "",
            f"<b>{escape(item.instrument.display_id)}</b>",
            f"현재 {format_pct(item.current_weight_pct)} · 목표 {format_pct(item.target_weight_pct)}",
            action,
        ]
    lines += [
        "",
        _DIVIDER,
        "",
        f"배분 합계 {format_money(plan.allocated_new_money.amount, plan.base_currency)}",
        f"현금 유지 {format_money(plan.unallocated_cash.amount, plan.base_currency)}",
        "",
        "매도를 제안하지 않고 신규 자금 배분을 우선했습니다.",
    ]
    return "\n".join(lines)


def split_telegram_message(text: str, *, limit: int = 3900) -> list[str]:
    """Split at line boundaries; never exceed Telegram's 4096-char limit."""

    if limit < 256 or limit > 4096:
        raise ValueError("limit must be between 256 and 4096")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks
