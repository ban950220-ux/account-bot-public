"""Async Telegram commands for the read-only portfolio application."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Callable, Protocol

from account_bot.analysis import TargetAllocation, suggest_new_money_rebalance
from account_bot.config import Settings
from account_bot.domain import AssetType, Currency, InstrumentKey, Money, instruments_compatible
from account_bot.portfolio import RecurringBuyPlan
from account_bot.reports.formatter import (
    format_accounts,
    format_analysis,
    format_assets,
    format_money,
    format_portfolio,
    format_rebalance,
    format_recurring,
    split_telegram_message,
)
from account_bot.reports.periodic import build_weekly_report
from account_bot.services import AccountBotService, CompanyAnalysisService, PortfolioRefresh

from .auth import authorized_handler, is_authorized_update

LOGGER = logging.getLogger(__name__)
_AMOUNT_RE = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<currency>USD|KRW)$", re.I)
_PERCENT_RE = re.compile(r"^(?P<weight>\d+(?:\.\d+)?)%$")


class RecurringPlanGateway(Protocol):
    async def list_plans(self, *, enabled_only: bool = False) -> tuple[RecurringBuyPlan, ...]: ...

    async def upsert_plan(
        self,
        *,
        market: str,
        ticker: str,
        amount: Decimal,
        currency: Currency,
        target_weight_pct: Decimal | None,
        memo: str = "",
    ) -> RecurringBuyPlan: ...

    async def set_thesis(self, *, market: str, ticker: str, thesis: str) -> None: ...

    async def apply_group_change(
        self,
        *,
        asset_type: AssetType,
        delta: Decimal | None,
        stop: bool,
        excluded: frozenset[str],
    ) -> tuple[RecurringBuyPlan, ...]: ...


class UnavailableRecurringPlanGateway:
    async def list_plans(self, *, enabled_only: bool = False) -> tuple[RecurringBuyPlan, ...]:
        return ()

    async def upsert_plan(self, **_: Any) -> RecurringBuyPlan:
        raise RuntimeError("local recurring-buy storage is unavailable")

    async def set_thesis(self, **_: Any) -> None:
        raise RuntimeError("local thesis storage is unavailable")

    async def apply_group_change(self, **_: Any) -> tuple[RecurringBuyPlan, ...]:
        raise RuntimeError("local recurring-buy storage is unavailable")


@dataclass(slots=True)
class TelegramCommandController:
    settings: Settings
    portfolios: AccountBotService
    recurring: RecurringPlanGateway
    analysis: CompanyAnalysisService | None = None
    repository: Any | None = None
    refresh_ttl: timedelta = timedelta(seconds=45)
    _last_refresh: PortfolioRefresh | None = field(init=False, default=None, repr=False)
    _last_refresh_at: datetime | None = field(init=False, default=None, repr=False)
    _last_all_refresh: PortfolioRefresh | None = field(init=False, default=None, repr=False)
    _last_all_refresh_at: datetime | None = field(init=False, default=None, repr=False)
    _refresh_lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._refresh_lock = asyncio.Lock()

    async def _refresh(self, *, force: bool = False, all_brokers: bool = False) -> PortfolioRefresh:
        now = datetime.now(timezone.utc)
        cached = self._last_all_refresh if all_brokers else self._last_refresh
        cached_at = self._last_all_refresh_at if all_brokers else self._last_refresh_at
        if (
            not force
            and cached is not None
            and cached_at is not None
            and now - cached_at < self.refresh_ttl
        ):
            return cached
        async with self._refresh_lock:
            now = datetime.now(timezone.utc)
            cached = self._last_all_refresh if all_brokers else self._last_refresh
            cached_at = self._last_all_refresh_at if all_brokers else self._last_refresh_at
            if (
                not force
                and cached is not None
                and cached_at is not None
                and now - cached_at < self.refresh_ttl
            ):
                return cached
            value = await self.portfolios.refresh_portfolio(
                broker_names=None if all_brokers else {"kiwoom_main"}
            )
            if all_brokers:
                self._last_all_refresh = value
                self._last_all_refresh_at = datetime.now(timezone.utc)
            else:
                self._last_refresh = value
                self._last_refresh_at = datetime.now(timezone.utc)
            return value

    @staticmethod
    def _args(update: Any) -> list[str]:
        message = getattr(update, "effective_message", None)
        text = str(getattr(message, "text", "") or "").strip()
        if not text:
            return []
        return text.split()[1:]

    async def _send(self, update: Any, text: str) -> None:
        message = getattr(update, "effective_message", None)
        if message is None:
            return
        for chunk in split_telegram_message(text):
            await message.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    async def start(self, update: Any, context: Any) -> None:
        await self._send(
            update,
            "🤖 <b>통합 계좌관리봇</b>\n\n"
            "전체자산 분석은 키움 메인 계좌만 사용합니다.\n"
            "키움 모으기·메리츠는 모으기 점검에서만 사용합니다.\n"
            "<b>READ ONLY</b> — 매수·매도·정정·취소 주문은 실행하지 않습니다.\n\n"
            "<code>/help</code>에서 명령어를 확인하세요.",
        )

    async def help(self, update: Any, context: Any) -> None:
        await self._send(
            update,
            "🤖 <b>통합 계좌관리봇 도움말</b>\n\n"
            "<b>운영 목적</b>\n"
            "1. 매일모으기에서 불필요한 종목 걸러내기\n"
            "2. 모으기 설정을 기준으로 하루·주간 매수금액 확인\n"
            "3. 지나친 섹터 쏠림 방지\n"
            "4. 매일모으기 종목·금액 밸런싱\n"
            "5. 모으기 제외 온주를 정기 분석해 기계적인 차익실현 판단 지원\n\n"
            "🌱 <b>매일모으기 관리</b>\n"
            "/recurring · /모으기\n"
            "키움·메리츠·추가 후보의 종목 평가, 주간 금액, 섹터 분산, 근거 있는 금액 조정안\n"
            "권장 금액은 개별 종목 Valuation을 기준으로 산정\n\n"
            "📊 <b>키움 메인·온주 관리</b>\n"
            "/assets · /자산 — 키움 메인 자산과 지역·집중도\n"
            "/portfolio · /포트폴리오 — 키움 메인 보유종목·비중·수익률\n"
            "/analyze AMD · /분석 AMD — 키움 메인 기준 종목 정량 분석\n"
            "/stock AMD · /종목 AMD — 현재가 확인\n"
            "/weekly · /주간리포트 — 키움 메인과 모으기 주간 점검\n\n"
            "🔧 <b>상태·고급 명령</b>\n"
            "/recurring_add AMD 2000KRW 8% — 분석용 로컬 계획 추가\n"
            "/recurring_update ETF +1000 — ETF 전체 금액 변경\n"
            "/모으기변경 개별주 NVDA,AMD 빼고 -1000 — 일부 제외 후 변경\n"
            "/모으기변경 ETF 전부 모으기중단 — 해당 그룹 중단\n"
            "/rebalance 500000 · /리밸런싱 500000 — 목표비중 기반 신규자금 의견\n"
            "/thesis AMD 투자논리 — 종목 Thesis 저장\n\n"
            "메리츠와 키움 모으기 보유자산은 전체계좌 평가에서 제외됩니다.\n"
            "한국 종목은 <code>KOSPI:005930</code>처럼 시장을 지정합니다.\n"
            "<b>READ ONLY</b> — 매수·매도·정정·취소 주문을 실행하지 않습니다.\n"
            "섹터·온주 자동 선별은 검증 가능한 데이터만 사용하며 부족하면 판단을 보류합니다.",
        )

    async def assets(self, update: Any, context: Any) -> None:
        await self._send(update, format_assets(await self._refresh(force=True)))

    async def accounts(self, update: Any, context: Any) -> None:
        await self._send(update, format_accounts(await self._refresh(force=True, all_brokers=True)))

    async def portfolio(self, update: Any, context: Any) -> None:
        refresh = await self._refresh(force=True)
        await self._send(update, format_portfolio(refresh.snapshot))

    async def recurring_list(self, update: Any, context: Any) -> None:
        refresh, plans = await asyncio.gather(
            self._refresh(), self.recurring.list_plans(enabled_only=True)
        )
        analyses = await self._analyze_plans(plans, refresh)
        await self._send(
            update,
            format_recurring(
                plans,
                refresh.snapshot,
                analyses,
            ),
        )

    async def recurring_add(self, update: Any, context: Any) -> None:
        args = self._args(update)
        if len(args) < 3:
            await self._send(update, "사용법: <code>/recurring_add AMD 20USD 8%</code>")
            return
        market, ticker = await self._instrument_parts(args[0])
        amount_match = _AMOUNT_RE.fullmatch(args[1])
        weight_match = _PERCENT_RE.fullmatch(args[2])
        if not amount_match or not weight_match:
            await self._send(update, "금액은 20USD/10000KRW, 목표는 8% 형식이어야 합니다.")
            return
        amount = Decimal(amount_match.group("amount"))
        currency = Currency(amount_match.group("currency").upper())
        weight = Decimal(weight_match.group("weight"))
        if amount <= 0 or weight < 0 or weight > 100:
            await self._send(update, "금액은 0보다 커야 하고 목표 비중은 0~100%여야 합니다.")
            return
        if currency == Currency.KRW and market not in {"KOSPI", "KOSDAQ", "KRX", "NXT"}:
            await self._send(update, "해외 종목 적립금은 USD로 입력해 주세요.")
            return
        if currency == Currency.USD and market in {"KOSPI", "KOSDAQ", "KRX", "NXT"}:
            await self._send(update, "한국 종목 적립금은 KRW로 입력해 주세요.")
            return
        memo = " ".join(args[3:])
        plan = await self.recurring.upsert_plan(
            market=market,
            ticker=ticker,
            amount=amount,
            currency=currency,
            target_weight_pct=weight,
            memo=memo,
        )
        await self._send(
            update,
            f"✅ <b>{escape(plan.instrument.display_id)}</b> 로컬 적립 계획을 저장했습니다.\n"
            f"매일 {format_money(plan.daily_amount.amount, plan.daily_amount.currency)} · 목표 {plan.target_weight_pct}%\n\n"
            "주문은 실행되지 않습니다.",
        )

    async def recurring_update(self, update: Any, context: Any) -> None:
        args = self._args(update)
        usage = (
            "사용법:\n"
            "<code>/모으기변경 ETF 전부 +1000</code>\n"
            "<code>/모으기변경 개별주 NVDA,AMD 빼고 -1000</code>\n"
            "<code>/모으기변경 ETF 전부 모으기중단</code>"
        )
        if len(args) < 2:
            await self._send(update, usage)
            return
        group = args[0].upper()
        if group == "ETF":
            asset_type = AssetType.ETF
            group_label = "ETF"
        elif group in {"개별주", "주식", "EQUITY"}:
            asset_type = AssetType.EQUITY
            group_label = "개별주"
        else:
            await self._send(update, "첫 항목은 ETF 또는 개별주여야 합니다.\n" + usage)
            return

        text = " ".join(args[1:]).strip()
        stop = bool(re.search(r"(?:모으기)?(?:중단|정지)$", text))
        delta: Decimal | None = None
        if not stop:
            match = re.search(r"(?P<delta>[+-]\d[\d,]*)(?:원)?$", text)
            if match is None:
                await self._send(update, usage)
                return
            delta = Decimal(match.group("delta").replace(",", ""))
            if delta == 0 or delta % Decimal("1000") != 0:
                await self._send(update, "변경 금액은 1,000원 단위의 0이 아닌 값이어야 합니다.")
                return

        excluded: frozenset[str] = frozenset()
        if "빼고" in text:
            prefix = text.split("빼고", 1)[0]
            tokens = re.split(r"[\s,]+", prefix.replace("전부", "").strip())
            excluded = frozenset(token.upper() for token in tokens if token)

        changed = await self.recurring.apply_group_change(
            asset_type=asset_type,
            delta=delta,
            stop=stop,
            excluded=excluded,
        )
        if not changed:
            await self._send(update, "조건에 맞는 변경 대상이 없습니다.")
            return
        action = "모으기 중단" if stop else f"{delta:+,.0f}원 변경"
        details = ", ".join(
            f"{plan.instrument.ticker}({format_money(plan.daily_amount.amount, plan.daily_amount.currency)})"
            for plan in sorted(changed, key=lambda item: item.instrument.ticker)
        )
        await self._send(
            update,
            f"✅ <b>{group_label} {len(changed)}종목</b> {action}\n{escape(details)}\n\n"
            "봇의 모으기 계획만 변경했으며 증권사 주문은 실행하지 않았습니다.",
        )

    async def stock(self, update: Any, context: Any) -> None:
        args = self._args(update)
        if not args:
            await self._send(update, "사용법: <code>/stock AMD</code>")
            return
        if self.analysis is None:
            await self._send(update, "시장데이터 provider가 설정되지 않았습니다.")
            return
        market, ticker = await self._instrument_parts(args[0])
        try:
            quote = await self.analysis.provider.get_price(market, ticker)
        except Exception:
            await self._send(update, "현재가를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.")
            return
        stale = " · 마지막 정상값" if quote.stale else ""
        await self._send(
            update,
            f"📈 <b>{escape(market)}:{escape(ticker)}</b>\n\n"
            f"현재가 {format_money(quote.price, quote.currency)}{stale}\n"
            f"기준 {quote.as_of:%Y-%m-%d %H:%M} UTC · {escape(quote.provider)}",
        )

    async def analyze(self, update: Any, context: Any) -> None:
        args = self._args(update)
        if not args:
            await self._send(update, "사용법: <code>/analyze AMD</code>")
            return
        if self.analysis is None:
            await self._send(update, "시장데이터 provider가 설정되지 않아 분석을 보류합니다.")
            return
        market, ticker = await self._instrument_parts(args[0])
        refresh, plans = await asyncio.gather(
            self._refresh(), self.recurring.list_plans(enabled_only=False)
        )
        target = next(
            (
                plan.target_weight_pct
                for plan in plans
                if instruments_compatible(
                    plan.instrument,
                    InstrumentKey(
                        market,
                        ticker,
                        plan.instrument.asset_type,
                        plan.instrument.currency,
                    ),
                )
            ),
            None,
        )
        result = await self.analysis.analyze(
            market=market,
            ticker=ticker,
            portfolio=refresh.snapshot,
            target_weight_pct=target,
        )
        await self._send(update, format_analysis(result))

    async def thesis(self, update: Any, context: Any) -> None:
        args = self._args(update)
        if len(args) < 2:
            await self._send(update, "사용법: <code>/thesis AMD 투자논리...</code>")
            return
        market, ticker = await self._instrument_parts(args[0])
        thesis = " ".join(args[1:]).strip()
        if len(thesis) > 4_000:
            await self._send(update, "Thesis는 4,000자 이내로 입력해 주세요.")
            return
        plans = await self.recurring.list_plans(enabled_only=False)
        matching_plans = [
            plan
            for plan in plans
            if instruments_compatible(
                plan.instrument,
                InstrumentKey(
                    market,
                    ticker,
                    plan.instrument.asset_type,
                    plan.instrument.currency,
                ),
            )
        ]
        if len(matching_plans) == 1:
            market = matching_plans[0].instrument.market
            ticker = matching_plans[0].instrument.ticker
        await self.recurring.set_thesis(market=market, ticker=ticker, thesis=thesis)
        await self._send(
            update,
            f"📝 <b>{escape(market)}:{escape(ticker)}</b> Thesis revision을 저장했습니다.",
        )

    async def rebalance(self, update: Any, context: Any) -> None:
        args = self._args(update)
        try:
            amount = Decimal(args[0].replace(",", "")) if args else Decimal("0")
        except (InvalidOperation, IndexError):
            await self._send(update, "사용법: <code>/rebalance 500000</code> (원화 신규 자금)")
            return
        if amount < 0:
            await self._send(update, "신규 자금은 0 이상이어야 합니다.")
            return
        refresh, plans = await asyncio.gather(
            self._refresh(), self.recurring.list_plans(enabled_only=True)
        )
        targets_list: list[TargetAllocation] = []
        for recurring_plan in plans:
            if recurring_plan.target_weight_pct is None:
                continue
            matches = [
                position.instrument
                for position in refresh.snapshot.positions
                if instruments_compatible(position.instrument, recurring_plan.instrument)
            ]
            instrument = matches[0] if len(matches) == 1 else recurring_plan.instrument
            targets_list.append(TargetAllocation(instrument, recurring_plan.target_weight_pct))
        targets = tuple(targets_list)
        if not targets:
            await self._send(update, "목표 비중이 있는 적립 계획을 먼저 등록해 주세요.")
            return
        plan = suggest_new_money_rebalance(
            refresh.snapshot,
            targets,
            Money(amount, refresh.snapshot.base_currency),
        )
        await self._send(update, format_rebalance(plan))

    async def weekly(self, update: Any, context: Any) -> None:
        refresh, plans = await asyncio.gather(
            self._refresh(force=True), self.recurring.list_plans(enabled_only=True)
        )
        analyses = await self._analyze_plans(plans, refresh)
        await self._send(
            update,
            build_weekly_report(refresh, recurring_plans=plans, analyses=analyses),
        )

    async def _instrument_parts(self, raw: str) -> tuple[str, str]:
        token = raw.strip().upper()
        if ":" in token:
            market, ticker = token.split(":", 1)
            if market and ticker:
                return market, ticker
        refresh = self._last_refresh
        if refresh is not None:
            matches = {
                position.instrument.market
                for position in refresh.snapshot.positions
                if position.instrument.ticker == token
            }
            if len(matches) == 1:
                return next(iter(matches)), token
        return ("KOSPI", token.zfill(6)) if token.isdigit() else ("NASDAQ", token)

    async def _analyze_plans(
        self,
        plans: tuple[RecurringBuyPlan, ...],
        refresh: PortfolioRefresh,
        extra_instruments: tuple[InstrumentKey, ...] = (),
    ) -> dict[str, Any]:
        if self.analysis is None:
            return {}
        semaphore = asyncio.Semaphore(4)

        unique_work: dict[str, tuple[InstrumentKey, Decimal | None]] = {}
        for plan in plans:
            prior = unique_work.get(plan.instrument.stable_id)
            target = plan.target_weight_pct
            if prior is None or (prior[1] is None and target is not None):
                unique_work[plan.instrument.stable_id] = (plan.instrument, target)
        for instrument in extra_instruments:
            unique_work.setdefault(instrument.stable_id, (instrument, None))
        work = tuple(unique_work.values())

        async def one(item: tuple[InstrumentKey, Decimal | None]):
            instrument, target_weight = item
            async with semaphore:
                try:
                    value = await self.analysis.analyze(
                        market=instrument.market,
                        ticker=instrument.ticker,
                        portfolio=refresh.snapshot,
                        target_weight_pct=target_weight,
                    )
                except Exception:
                    return instrument.stable_id, None
                return instrument.stable_id, value

        pairs = await asyncio.gather(*(one(item) for item in work))
        return {key: value for key, value in pairs if value is not None}


def register_handlers(
    application: Any,
    controller: TelegramCommandController,
    *,
    accepting: Callable[[], bool] | None = None,
) -> None:
    """Register English Bot API commands and Korean text-command aliases."""

    from telegram.error import NetworkError
    from telegram.ext import CommandHandler, MessageHandler, filters

    allowed = controller.settings.allowed_telegram_user_ids
    commands = {
        "start": controller.start,
        "help": controller.help,
        "assets": controller.assets,
        "portfolio": controller.portfolio,
        "recurring": controller.recurring_list,
        "recurring_add": controller.recurring_add,
        "recurring_update": controller.recurring_update,
        "stock": controller.stock,
        "analyze": controller.analyze,
        "thesis": controller.thesis,
        "rebalance": controller.rebalance,
        "weekly": controller.weekly,
    }
    for name, callback in commands.items():
        application.add_handler(
            CommandHandler(name, authorized_handler(allowed, callback, accepting=accepting))
        )

    korean = {
        "도움말": controller.help,
        "자산": controller.assets,
        "포트폴리오": controller.portfolio,
        "모으기": controller.recurring_list,
        "모으기추가": controller.recurring_add,
        "모으기변경": controller.recurring_update,
        "종목": controller.stock,
        "분석": controller.analyze,
        "리밸런싱": controller.rebalance,
        "주간리포트": controller.weekly,
    }
    for name, callback in korean.items():
        pattern = rf"^/{name}(?:@\w+)?(?:\s|$)"
        application.add_handler(
            MessageHandler(
                filters.Regex(pattern), authorized_handler(allowed, callback, accepting=accepting)
            )
        )

    async def on_error(update: Any, context: Any) -> None:
        error = getattr(context, "error", None)
        if isinstance(error, NetworkError):
            LOGGER.warning("telegram transient network failure error_type=%s", type(error).__name__)
            return
        LOGGER.error(
            "telegram handler failed error_type=%s",
            type(error).__name__ if error is not None else "UnknownError",
        )
        if update is not None and is_authorized_update(update, allowed):
            message = getattr(update, "effective_message", None)
            if message is not None:
                await message.reply_text("요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")

    application.add_error_handler(on_error)
