from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from account_bot.app import build_runtime, build_telegram_application
from account_bot.config import Settings
from account_bot.local_control import LocalRuntimeControl


class ApplicationCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_toss_is_excluded_from_expected_sources(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(environ={}, base_dir=Path(temporary_directory))
            runtime = build_runtime(settings)
            try:
                self.assertNotIn("toss", {broker.name for broker in runtime.brokers})
            finally:
                for broker in runtime.brokers:
                    await broker.aclose()
                await runtime.database.dispose()

    async def test_builds_distinct_main_and_accum_kiwoom_sources(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "KIWOOM_MAIN_APPKEY": "main-key",
                    "KIWOOM_MAIN_SECRET": "main-secret",
                    "KIWOOM_ACCUM_APPKEY": "accum-key",
                    "KIWOOM_ACCUM_SECRET": "accum-secret",
                    "KIWOOM_API_BASE_URL": "https://mockapi.kiwoom.com",
                },
                base_dir=Path(temporary_directory),
            )
            runtime = build_runtime(settings)
            try:
                self.assertEqual(
                    tuple(broker.name for broker in runtime.brokers[:2]),
                    ("kiwoom_main", "kiwoom_accum"),
                )
                self.assertEqual(runtime.brokers[0]._account_id, "kiwoom-main")
                self.assertEqual(runtime.brokers[1]._account_id, "kiwoom-accum")
            finally:
                for broker in runtime.brokers:
                    await broker.aclose()
                await runtime.database.dispose()

    async def test_official_kiwoom_mock_origin_enables_mock_query_mode(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "KIWOOM_APP_KEY": "fixture-key",
                    "KIWOOM_APP_SECRET": "fixture-secret",
                    "KIWOOM_API_BASE_URL": "https://mockapi.kiwoom.com",
                },
                base_dir=Path(temporary_directory),
            )
            runtime = build_runtime(settings)

            self.assertTrue(runtime.brokers[0]._mock)

            for broker in runtime.brokers:
                await broker.aclose()
            await runtime.database.dispose()

    async def test_builds_separate_authorized_bot_with_single_job_queue(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "ACCOUNT_BOT_TELEGRAM_TOKEN": "123456:fixture-token",
                    "ALLOWED_TELEGRAM_USER_IDS": "1001",
                    "ACCOUNT_BOT_FINGERPRINT_PEPPER": "fixture-pepper-long-value",
                    "SCHEDULER_ENABLED": "true",
                },
                base_dir=Path(temporary_directory),
            )
            runtime = build_runtime(settings)
            application = build_telegram_application(runtime)

            command_names = {
                command
                for handlers in application.handlers.values()
                for handler in handlers
                for command in getattr(handler, "commands", ())
            }
            self.assertTrue(
                {
                    "assets",
                    "portfolio",
                    "recurring",
                    "recurring_add",
                    "stock",
                    "analyze",
                    "thesis",
                    "rebalance",
                    "weekly",
                    "help",
                }.issubset(command_names)
            )
            self.assertIsNotNone(application.job_queue)
            self.assertEqual(
                {job.name for job in application.job_queue.jobs()},
                {
                    "account-bot-daily-report",
                    "account-bot-weekly-valuation",
                    "account-bot-weekly-report",
                },
            )

            for broker in runtime.brokers:
                await broker.aclose()
            await runtime.database.dispose()

    async def test_local_control_uses_existing_job_queue_without_network_listener(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "ACCOUNT_BOT_TELEGRAM_TOKEN": "123456:fixture-token",
                    "ALLOWED_TELEGRAM_USER_IDS": "1001",
                },
                base_dir=Path(temporary_directory),
            )
            runtime = build_runtime(settings)
            control = LocalRuntimeControl(settings.data_dir)
            application = build_telegram_application(runtime, local_control=control)

            self.assertIn(
                "account-bot-local-control",
                {job.name for job in application.job_queue.jobs()},
            )
            for broker in runtime.brokers:
                await broker.aclose()
            await runtime.database.dispose()

    async def test_shutdown_closes_broker_and_db_before_recording_stopped(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "ACCOUNT_BOT_TELEGRAM_TOKEN": "123456:fixture-token",
                    "ALLOWED_TELEGRAM_USER_IDS": "1001",
                },
                base_dir=Path(temporary_directory),
            )
            settings.data_dir.mkdir()
            runtime = build_runtime(settings)
            original_brokers = runtime.brokers
            order: list[str] = []

            class FakeBroker:
                async def aclose(self) -> None:
                    order.append("broker")

            async def dispose() -> None:
                order.append("database")

            runtime.brokers = (FakeBroker(),)  # type: ignore[assignment]
            control = LocalRuntimeControl(settings.data_dir)
            control.start()
            original_finish = control.finish

            def finish(*, clean: bool = True) -> None:
                order.append("status")
                original_finish(clean=clean)

            control.finish = finish  # type: ignore[method-assign]
            application = build_telegram_application(runtime, local_control=control)
            try:
                with patch.object(runtime.database, "dispose", new=AsyncMock(side_effect=dispose)):
                    await application.post_shutdown(application)
                self.assertEqual(order, ["broker", "database", "status"])
                self.assertEqual(control.state, "stopped")
            finally:
                for broker in original_brokers:
                    await broker.aclose()
                await runtime.database.dispose()

    async def test_cancelled_database_cleanup_cannot_report_normal_shutdown(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = Settings.load(
                environ={
                    "ACCOUNT_BOT_TELEGRAM_TOKEN": "123456:fixture-token",
                    "ALLOWED_TELEGRAM_USER_IDS": "1001",
                },
                base_dir=Path(temporary_directory),
            )
            settings.data_dir.mkdir()
            runtime = build_runtime(settings)
            control = LocalRuntimeControl(settings.data_dir)
            control.start()
            application = build_telegram_application(runtime, local_control=control)
            try:
                with patch.object(
                    runtime.database,
                    "dispose",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await application.post_shutdown(application)
                self.assertEqual(control.state, "cleanup_failed")
            finally:
                await runtime.database.dispose()


if __name__ == "__main__":
    unittest.main()
