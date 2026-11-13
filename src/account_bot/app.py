"""Application composition root; existing bots are never imported or modified."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from decimal import Decimal

from account_bot.analysis import ScoringWeights
from account_bot.brokers import (
    JsonSnapshotBroker,
    KiwoomBroker,
    MeritzBroker,
    ReadOnlyBroker,
    TossBroker,
)
from account_bot.config import Settings, SettingsError
from account_bot.local_control import LocalRuntimeControl
from account_bot.market import CachedMarketDataProvider, YFinanceProvider
from account_bot.polling_guard import require_polling_ownership
from account_bot.runtime_lock import single_instance
from account_bot.scheduler import ScheduledReportRunner, register_scheduled_reports
from account_bot.services import AccountBotService, CompanyAnalysisService
from account_bot.services.storage_adapters import (
    SnapshotAugmentedRecurringPlanGateway,
    SqlAlchemyFxRateStore,
    SqlAlchemyRecurringPlanGateway,
)
from account_bot.storage import Database, SQLitePortfolioRepository
from account_bot.telegram_ui import (
    RecurringPlanGateway,
    TelegramCommandController,
    register_handlers,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationRuntime:
    settings: Settings
    database: Database
    repository: SQLitePortfolioRepository
    brokers: tuple[ReadOnlyBroker, ...]
    portfolios: AccountBotService
    recurring: RecurringPlanGateway
    analysis: CompanyAnalysisService | None


def _build_brokers(settings: Settings) -> tuple[ReadOnlyBroker, ...]:
    brokers: list[ReadOnlyBroker] = []
    if settings.kiwoom_main_app_key and settings.kiwoom_main_app_secret:
        kiwoom_main: ReadOnlyBroker = KiwoomBroker(
            app_key=settings.kiwoom_main_app_key,
            app_secret=settings.kiwoom_main_app_secret,
            broker_name="kiwoom_main",
            account_id="kiwoom-main",
            account_alias="키움 메인 계좌",
            mock=settings.kiwoom_api_base_url == "https://mockapi.kiwoom.com",
            base_url=settings.kiwoom_api_base_url,
        )
    else:
        kiwoom_main = JsonSnapshotBroker("kiwoom_main", settings.kiwoom_snapshot_path)
    brokers.append(kiwoom_main)

    if settings.kiwoom_accum_app_key and settings.kiwoom_accum_app_secret:
        kiwoom_accum: ReadOnlyBroker = KiwoomBroker(
            app_key=settings.kiwoom_accum_app_key,
            app_secret=settings.kiwoom_accum_app_secret,
            broker_name="kiwoom_accum",
            account_id="kiwoom-accum",
            account_alias="키움 모으기 계좌",
            mock=settings.kiwoom_api_base_url == "https://mockapi.kiwoom.com",
            base_url=settings.kiwoom_api_base_url,
        )
    else:
        kiwoom_accum = JsonSnapshotBroker("kiwoom_accum", None)
    brokers.append(kiwoom_accum)

    if settings.toss_client_id and settings.toss_client_secret:
        toss: ReadOnlyBroker = TossBroker(
            client_id=settings.toss_client_id,
            client_secret=settings.toss_client_secret,
            base_url=settings.toss_api_base_url,
        )
        brokers.append(toss)
    elif settings.toss_snapshot_path is not None:
        toss = JsonSnapshotBroker("toss", settings.toss_snapshot_path)
        brokers.append(toss)

    meritz: ReadOnlyBroker = MeritzBroker(settings.meritz_snapshot_path)
    brokers.append(meritz)
    return tuple(brokers)


def build_runtime(settings: Settings) -> ApplicationRuntime:
    database = Database(
        settings.database_url,
        sqlite_busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    repository = SQLitePortfolioRepository(database)
    recurring = SnapshotAugmentedRecurringPlanGateway(
        SqlAlchemyRecurringPlanGateway(
            repository,
            timezone_name=settings.timezone_name,
        ),
        settings.meritz_accumul_snapshot_path,
    )
    market_provider = None
    if importlib.util.find_spec("yfinance") is not None:
        market_provider = CachedMarketDataProvider(YFinanceProvider())

    def percent(value: float) -> Decimal:
        return Decimal(str(value)) * Decimal("100")

    scoring_weights = ScoringWeights(
        fundamental=percent(settings.company_weights.fundamental),
        valuation=percent(settings.company_weights.valuation),
        growth=percent(settings.company_weights.growth),
        moat=percent(settings.company_weights.moat),
        safety=percent(settings.company_weights.risk),
        company_final=percent(settings.final_weights.company),
        portfolio_final=percent(settings.final_weights.portfolio),
    )
    analysis = (
        CompanyAnalysisService(market_provider, weights=scoring_weights)
        if market_provider
        else None
    )
    brokers = _build_brokers(settings)
    portfolios = AccountBotService(
        brokers,
        market_provider=market_provider,
        fx_store=SqlAlchemyFxRateStore(repository),
    )
    return ApplicationRuntime(
        settings=settings,
        database=database,
        repository=repository,
        brokers=brokers,
        portfolios=portfolios,
        recurring=recurring,
        analysis=analysis,
    )


def build_telegram_application(
    runtime: ApplicationRuntime, *, local_control: LocalRuntimeControl | None = None
):
    from telegram import BotCommand
    from telegram.constants import ParseMode
    from telegram.ext import Application, Defaults

    settings = runtime.settings
    if not settings.telegram_token:
        raise SettingsError("ACCOUNT_BOT_TELEGRAM_TOKEN is required to run the bot")
    if not settings.allowed_telegram_user_ids:
        raise SettingsError("ALLOWED_TELEGRAM_USER_IDS is required and may not be empty")

    defaults = Defaults(tzinfo=settings.timezone, parse_mode=ParseMode.HTML)

    async def post_init(application) -> None:
        # PTB starts its polling bootstrap after post_init. Refuse ownership
        # before database writes, commands, scheduler startup or deleteWebhook.
        await require_polling_ownership(application.bot)
        await runtime.database.initialize()
        await runtime.repository.initialize_default_portfolio()
        await application.bot.set_my_commands(
            [
                BotCommand("recurring", "모으기 평가·금액조정·섹터·요약"),
                BotCommand("recurring_update", "ETF·개별주 모으기 일괄변경"),
                BotCommand("assets", "키움 메인 전체자산·집중도"),
                BotCommand("portfolio", "키움 메인 보유종목·비중"),
                BotCommand("analyze", "키움 메인 기준 종목 분석"),
                BotCommand("weekly", "키움 메인·모으기 주간점검"),
                BotCommand("stock", "종목 현재가"),
                BotCommand("help", "범위와 전체 명령어"),
            ]
        )
        for broker in runtime.brokers:
            LOGGER.info(
                "broker source status broker=%s configured=%s",
                broker.name,
                broker.configured,
            )

    async def post_shutdown(application) -> None:
        del application
        cleanup_failed = False
        try:
            for broker in runtime.brokers:
                try:
                    await broker.aclose()
                except Exception as exc:
                    cleanup_failed = True
                    LOGGER.warning(
                        "broker shutdown failed broker=%s error_type=%s",
                        broker.name,
                        type(exc).__name__,
                    )
        except BaseException:
            cleanup_failed = True
            raise
        finally:
            try:
                await runtime.database.dispose()
            except BaseException:
                cleanup_failed = True
                raise
            finally:
                if local_control is not None:
                    local_control.finish(clean=not cleanup_failed)

    application = (
        Application.builder()
        .token(settings.telegram_token)
        .defaults(defaults)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    controller = TelegramCommandController(
        settings=settings,
        portfolios=runtime.portfolios,
        recurring=runtime.recurring,
        analysis=runtime.analysis,
        repository=runtime.repository,
    )
    register_handlers(
        application,
        controller,
        accepting=(lambda: local_control.accepting) if local_control is not None else None,
    )
    runner = ScheduledReportRunner(
        settings=settings,
        repository=runtime.repository,
        portfolios=runtime.portfolios,
        recurring=runtime.recurring,
        analysis=runtime.analysis,
        accepting=(lambda: local_control.accepting) if local_control is not None else None,
    )
    register_scheduled_reports(application, runner)
    if local_control is not None:
        if application.job_queue is None:
            raise RuntimeError("local shutdown control requires PTB JobQueue")
        application.job_queue.run_repeating(
            local_control.poll,
            interval=1,
            first=0.25,
            name="account-bot-local-control",
        )
    return application


def run_polling(settings: Settings) -> None:
    local_control = LocalRuntimeControl(settings.data_dir)
    with single_instance(local_control.lease_path):
        local_control.start()
        runtime = build_runtime(settings)
        application = build_telegram_application(runtime, local_control=local_control)
        application.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
