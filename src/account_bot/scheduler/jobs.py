"""PTB JobQueue scheduling, snapshot persistence, and safe report delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any, Callable

from account_bot.config import Settings
from account_bot.domain import BrokerResult, BrokerStatus, Currency
from account_bot.reports import build_periodic_report, split_telegram_message
from account_bot.services import AccountBotService, CompanyAnalysisService, PortfolioRefresh
from account_bot.storage import SQLitePortfolioRepository, derive_fingerprint
from account_bot.telegram_ui.handlers import RecurringPlanGateway

LOGGER = logging.getLogger(__name__)


def _source_status(result: BrokerResult[Any]) -> tuple[str, str | None]:
    if result.status == BrokerStatus.OK:
        return "SUCCESS", None
    if result.status == BrokerStatus.STALE:
        return "STALE", None
    if result.status == BrokerStatus.NOT_CONFIGURED:
        return "UNSUPPORTED", None
    raw_error_type = result.errors[0].code if result.errors else result.status.name
    error_type = re.sub(r"[^A-Za-z0-9_.]", "_", raw_error_type).strip("_")
    if result.status == BrokerStatus.PARTIAL:
        return "PARTIAL", (error_type or "BROKER_PARTIAL")[:96]
    return "FAILED", (error_type or "BROKER_FAILURE")[:96]


@dataclass(slots=True)
class ScheduledReportRunner:
    settings: Settings
    repository: SQLitePortfolioRepository
    portfolios: AccountBotService
    recurring: RecurringPlanGateway
    analysis: CompanyAnalysisService | None = None
    accepting: Callable[[], bool] | None = None

    async def daily(self, context: Any) -> None:
        await self._run(context, "daily")

    async def weekly(self, context: Any) -> None:
        await self._run(context, "weekly")

    async def weekly_valuation(self, context: Any) -> None:
        """Refresh recurring valuations after the US Friday close."""

        del context
        if self.accepting is not None and not self.accepting():
            LOGGER.info("weekly valuation skipped state=stopping")
            return
        if self.analysis is None:
            LOGGER.warning("weekly valuation skipped error_type=AnalysisUnavailable")
            return
        try:
            plans = await self.recurring.list_plans(enabled_only=True)
            provider = self.analysis.provider
            cache = getattr(provider, "cache", None)
            clear = getattr(cache, "clear", None)
            if callable(clear):
                clear("valuation:")
            instruments = {
                plan.instrument.stable_id: plan.instrument for plan in plans
            }
            semaphore = asyncio.Semaphore(4)

            async def refresh(instrument: Any) -> bool:
                async with semaphore:
                    try:
                        await provider.get_valuation(
                            instrument.market,
                            instrument.ticker,
                        )
                        return True
                    except Exception:
                        return False

            refreshed = await asyncio.gather(
                *(refresh(instrument) for instrument in instruments.values())
            )
            successes = sum(refreshed)
            LOGGER.info(
                "weekly valuation refresh completed success=%s failed=%s basis=friday_close",
                successes,
                len(refreshed) - successes,
            )
        except Exception as exc:
            LOGGER.error(
                "weekly valuation refresh failed error_type=%s",
                type(exc).__name__,
            )

    async def _run(self, context: Any, report_type: str) -> None:
        if self.accepting is not None and not self.accepting():
            LOGGER.info("scheduled report skipped report_type=%s state=stopping", report_type)
            return
        pepper = self.settings.fingerprint_pepper
        if not pepper:
            LOGGER.error("scheduled report skipped error_type=MissingFingerprintPepper")
            return
        try:
            refresh, plans = await asyncio.gather(
                self.portfolios.refresh_portfolio(broker_names={"kiwoom_main"}),
                self.recurring.list_plans(enabled_only=True),
            )
            analyses = await self._analyze(plans, refresh)
            content = build_periodic_report(
                report_type, refresh, recurring_plans=plans, analyses=analyses
            )
            period_key = refresh.collected_at.astimezone(self.settings.timezone).date().isoformat()
        except Exception as exc:
            LOGGER.error(
                "scheduled report generation failed report_type=%s error_type=%s",
                report_type,
                type(exc).__name__,
            )
            return

        try:
            await self._persist_snapshot(
                refresh,
                report_type=report_type,
                period_key=period_key,
            )
        except Exception as exc:
            LOGGER.error(
                "scheduled snapshot persistence failed error_type=%s",
                type(exc).__name__,
            )

        for user_id in sorted(self.settings.allowed_telegram_user_ids):
            await self._deliver(
                context,
                report_type=report_type,
                period_key=period_key,
                user_id=user_id,
                content=content,
            )

    async def _deliver(
        self,
        context: Any,
        *,
        report_type: str,
        period_key: str,
        user_id: int,
        content: str,
    ) -> None:
        pepper = self.settings.fingerprint_pepper
        if not pepper:
            return
        audience = derive_fingerprint(str(user_id), pepper=pepper, namespace="tg_user")
        idempotency_key = f"{report_type}:{period_key}:{audience}:v1"
        try:
            report_claim = await self.repository.claim_report(
                idempotency_key=idempotency_key,
                report_type=report_type,
                period_key=period_key,
                audience_fingerprint=audience,
                timezone_name=self.settings.timezone_name,
            )
            claim_state = getattr(report_claim, "state", None)
            if not report_claim.created and claim_state not in {"CLAIMED", "READY"}:
                return
            if report_claim.created or claim_state == "CLAIMED":
                input_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                await self.repository.complete_report(
                    report_id=report_claim.record_id,
                    input_hash=input_hash,
                    content=content,
                )
            delivery_claim = await self.repository.claim_report_delivery(
                report_id=report_claim.record_id,
                channel="telegram",
                destination_fingerprint=audience,
            )
            if not delivery_claim.created:
                return
        except Exception as exc:
            LOGGER.error(
                "scheduled report claim failed report_type=%s error_type=%s",
                report_type,
                type(exc).__name__,
            )
            return

        try:
            for chunk in split_telegram_message(content):
                await context.bot.send_message(
                    chat_id=user_id,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        except Exception as exc:
            # Telegram has no idempotency key. If transport state is uncertain,
            # do not auto-retry and risk duplicate account reports.
            try:
                await self.repository.mark_report_delivery(
                    delivery_id=delivery_claim.record_id,
                    status="UNKNOWN",
                    safe_error_type=type(exc).__name__,
                )
            except Exception as state_exc:
                LOGGER.error(
                    "scheduled delivery state write failed error_type=%s",
                    type(state_exc).__name__,
                )
            finally:
                LOGGER.error(
                    "scheduled report delivery uncertain report_type=%s error_type=%s",
                    report_type,
                    type(exc).__name__,
                )
            return
        try:
            await self.repository.mark_report_delivery(
                delivery_id=delivery_claim.record_id,
                status="SENT",
            )
        except Exception as exc:
            LOGGER.error(
                "scheduled delivery state write failed error_type=%s",
                type(exc).__name__,
            )

    async def _analyze(self, plans: tuple[Any, ...], refresh: PortfolioRefresh) -> dict[str, Any]:
        if self.analysis is None:
            return {}
        semaphore = asyncio.Semaphore(4)

        unique_work: dict[str, tuple[Any, Any]] = {}
        for plan in plans:
            prior = unique_work.get(plan.instrument.stable_id)
            if prior is None or (prior[1] is None and plan.target_weight_pct is not None):
                unique_work[plan.instrument.stable_id] = (
                    plan.instrument,
                    plan.target_weight_pct,
                )
        known = set(unique_work)
        for position in refresh.snapshot.positions:
            if (
                any(item.broker == "kiwoom_accum" for item in position.broker_quantities)
                and position.instrument.stable_id not in known
            ):
                unique_work[position.instrument.stable_id] = (position.instrument, None)

        work = tuple(unique_work.values())

        async def one(item: tuple[Any, Any]) -> tuple[str, Any | None]:
            instrument, target_weight = item
            async with semaphore:
                try:
                    result = await self.analysis.analyze(
                        market=instrument.market,
                        ticker=instrument.ticker,
                        portfolio=refresh.snapshot,
                        target_weight_pct=target_weight,
                    )
                except Exception:
                    return instrument.stable_id, None
                return instrument.stable_id, result

        pairs = await asyncio.gather(*(one(item) for item in work))
        return {key: value for key, value in pairs if value is not None}

    async def _persist_snapshot(
        self,
        refresh: PortfolioRefresh,
        *,
        report_type: str,
        period_key: str,
    ) -> None:
        pepper = self.settings.fingerprint_pepper
        if not pepper:
            return
        claim = await self.repository.claim_snapshot(
            idempotency_key=f"scheduled:{report_type}:{period_key}:v1",
            captured_at=refresh.collected_at,
            data_as_of=refresh.collected_at,
            base_currency=refresh.snapshot.base_currency.value,
        )
        if not claim.created and getattr(claim, "state", None) != "COLLECTING":
            return

        account_ids: dict[tuple[str, str], int] = {}

        async def account_row(
            broker: str,
            account_id: str,
            currency: Currency,
        ) -> int:
            key = (broker, account_id)
            if key in account_ids:
                return account_ids[key]
            fingerprint = derive_fingerprint(
                account_id,
                pepper=pepper,
                namespace="account",
            )
            label = {"kiwoom": "키움 계좌", "toss": "토스 계좌", "meritz": "메리츠 계좌"}.get(
                broker, "등록 계좌"
            )
            row = await self.repository.upsert_account(
                broker=broker,
                account_fingerprint=fingerprint,
                base_currency=currency.value,
                masked_label=label,
            )
            account_ids[key] = row.id
            return row.id

        for result in refresh.account_results:
            for account in result.items:
                await account_row(result.broker, account.account_id, account.base_currency)

        all_source_results = tuple(
            (f"{result.broker}:positions", result) for result in refresh.position_results
        ) + tuple((f"{result.broker}:cash", result) for result in refresh.cash_results)
        for source_key, result in all_source_results:
            status, error_type = _source_status(result)
            await self.repository.record_snapshot_source(
                snapshot_id=claim.record_id,
                source_key=source_key,
                status=status,
                observed_at=result.as_of,
                safe_error_type=error_type,
            )

        fx_rate = refresh.fx_quote.rate if refresh.fx_quote is not None else None
        for result in refresh.position_results:
            for position in result.items:
                row_id = await account_row(
                    position.broker,
                    position.account_id,
                    position.instrument.currency,
                )
                local_value = position.effective_market_value
                value_in_base: Decimal | None = None
                if local_value is not None:
                    if position.instrument.currency == refresh.snapshot.base_currency:
                        value_in_base = local_value
                    elif (
                        position.instrument.currency == Currency.USD
                        and refresh.snapshot.base_currency == Currency.KRW
                        and fx_rate is not None
                    ):
                        value_in_base = local_value * fx_rate
                await self.repository.add_position(
                    snapshot_id=claim.record_id,
                    account_id=row_id,
                    market=position.instrument.market,
                    ticker=position.instrument.ticker,
                    currency=position.instrument.currency.value,
                    asset_type=position.instrument.asset_type.value,
                    quantity=position.quantity,
                    average_price=position.average_price,
                    current_price=position.current_price,
                    cost_basis=position.effective_cost_basis,
                    market_value=local_value,
                    unrealized_profit=position.effective_unrealized_profit,
                    return_pct=position.effective_return_pct,
                    price_as_of=position.as_of,
                    value_in_base=value_in_base,
                    company_name=position.company_name,
                )

        for result in refresh.cash_results:
            for cash in result.items:
                row_id = await account_row(cash.broker, cash.account_id, cash.currency)
                value_in_base: Decimal | None = None
                if cash.currency == refresh.snapshot.base_currency:
                    value_in_base = cash.amount
                elif (
                    cash.currency == Currency.USD
                    and refresh.snapshot.base_currency == Currency.KRW
                    and fx_rate is not None
                ):
                    value_in_base = cash.amount * fx_rate
                await self.repository.add_cash(
                    snapshot_id=claim.record_id,
                    account_id=row_id,
                    currency=cash.currency.value,
                    amount=cash.amount,
                    value_in_base=value_in_base,
                )

        snapshot = refresh.snapshot
        usable_source = any(
            result.status in {BrokerStatus.OK, BrokerStatus.PARTIAL, BrokerStatus.STALE}
            for _key, result in all_source_results
        )
        status = "COMPLETE" if snapshot.is_complete else "PARTIAL" if usable_source else "FAILED"
        total = snapshot.total_market_value.amount if snapshot.total_market_value else None
        cost = snapshot.total_cost_basis.amount if snapshot.total_cost_basis else None
        profit = total - cost if total is not None and cost is not None else None
        return_pct = (
            profit / cost * Decimal("100")
            if profit is not None and cost not in {None, Decimal("0")}
            else None
        )
        await self.repository.finalize_snapshot(
            snapshot_id=claim.record_id,
            status=status,
            total_market_value=total,
            total_cost_basis=cost,
            total_unrealized_profit=profit,
            total_return_pct=return_pct,
        )


def register_scheduled_reports(application: Any, runner: ScheduledReportRunner) -> bool:
    """Register jobs in PTB's single AsyncIOScheduler instance."""

    settings = runner.settings
    if (
        not settings.scheduler_enabled
        or not settings.telegram_enabled
        or not settings.fingerprint_pepper
    ):
        return False
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError("python-telegram-bot job-queue extra is required")

    daily_time = settings.daily_report_time.replace(tzinfo=settings.timezone)
    weekly_time = settings.weekly_report_time.replace(tzinfo=settings.timezone)
    job_options = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    # PTB uses Sunday=0; settings use Python's Monday=0 convention.
    job_queue.run_daily(
        runner.daily,
        time=daily_time,
        days=(1, 2, 3, 4, 5),
        name="account-bot-daily-report",
        job_kwargs=job_options,
    )
    job_queue.run_daily(
        runner.weekly_valuation,
        time=time(8, 30, tzinfo=settings.timezone),
        days=(6,),
        name="account-bot-weekly-valuation",
        job_kwargs=job_options,
    )
    job_queue.run_daily(
        runner.weekly,
        time=weekly_time,
        days=((settings.weekly_report_weekday + 1) % 7,),
        name="account-bot-weekly-report",
        job_kwargs=job_options,
    )
    return True
