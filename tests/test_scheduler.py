from __future__ import annotations

import hashlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Scheduler annotations import the concrete storage package.  This temporary
# protocol-shaped stub keeps these unit tests independent from SQLAlchemy.
storage_stub = types.ModuleType("account_bot.storage")


class SQLitePortfolioRepositoryStub:
    pass


def fake_derive_fingerprint(value: str, *, pepper: str, namespace: str) -> str:
    payload = f"{namespace}:{pepper}:{value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


storage_stub.SQLitePortfolioRepository = SQLitePortfolioRepositoryStub
storage_stub.derive_fingerprint = fake_derive_fingerprint
with patch.dict(sys.modules, {"account_bot.storage": storage_stub}):
    from account_bot.scheduler import (  # noqa: E402
        ScheduledReportRunner,
        register_scheduled_reports,
    )
    from account_bot.scheduler.jobs import _source_status  # noqa: E402

# Do not leave the temporary child module attached to the account_bot package;
# later storage tests must import the real implementation (or skip normally).
import account_bot  # noqa: E402

if getattr(account_bot, "storage", None) is storage_stub:
    delattr(account_bot, "storage")

from account_bot.config import Settings  # noqa: E402


def make_settings(**overrides: str) -> Settings:
    environment = {
        "SCHEDULER_ENABLED": "true",
        "ACCOUNT_BOT_TELEGRAM_TOKEN": "fixture-telegram-token",
        "ACCOUNT_BOT_FINGERPRINT_PEPPER": "fixture-fingerprint-pepper",
        "ALLOWED_TELEGRAM_USER_IDS": "1001",
    }
    environment.update(overrides)
    return Settings.load(environ=environment, base_dir=PROJECT_ROOT)


class FakeRepository:
    def __init__(self) -> None:
        self.report_ids: dict[str, int] = {}
        self.report_states: dict[str, str] = {}
        self.delivery_ids: dict[tuple[int, str, str], int] = {}
        self.completed: list[dict] = []
        self.delivery_marks: list[dict] = []
        self.report_claim_calls = 0

    async def claim_report(self, *, idempotency_key: str, **kwargs):
        self.report_claim_calls += 1
        if idempotency_key in self.report_ids:
            return SimpleNamespace(
                created=False,
                record_id=self.report_ids[idempotency_key],
                state=self.report_states[idempotency_key],
            )
        record_id = len(self.report_ids) + 1
        self.report_ids[idempotency_key] = record_id
        self.report_states[idempotency_key] = "CLAIMED"
        return SimpleNamespace(created=True, record_id=record_id, state="CLAIMED")

    async def complete_report(self, **kwargs):
        self.completed.append(kwargs)
        report_id = kwargs["report_id"]
        key = next(key for key, value in self.report_ids.items() if value == report_id)
        self.report_states[key] = "READY"

    async def claim_report_delivery(
        self,
        *,
        report_id: int,
        channel: str,
        destination_fingerprint: str,
    ):
        key = (report_id, channel, destination_fingerprint)
        if key in self.delivery_ids:
            return SimpleNamespace(created=False, record_id=self.delivery_ids[key])
        record_id = len(self.delivery_ids) + 101
        self.delivery_ids[key] = record_id
        return SimpleNamespace(created=True, record_id=record_id)

    async def mark_report_delivery(self, **kwargs):
        self.delivery_marks.append(kwargs)


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        if self.fail:
            raise RuntimeError("fixture Telegram transport failure")


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def run_daily(self, callback, **kwargs):
        self.jobs.append({"callback": callback, **kwargs})


class ScheduledReportRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_skips_new_report_and_valuation_work(self) -> None:
        repository = FakeRepository()
        runner = self.runner(repository)
        runner.accepting = lambda: False

        await runner.daily(SimpleNamespace(bot=FakeBot()))
        await runner.weekly(SimpleNamespace(bot=FakeBot()))
        await runner.weekly_valuation(None)

        self.assertEqual(repository.report_claim_calls, 0)

    def test_partial_broker_result_keeps_distinct_persistence_status(self) -> None:
        from account_bot.domain import BrokerError, BrokerResult, BrokerStatus

        result = BrokerResult.failure(
            "kiwoom",
            BrokerStatus.PARTIAL,
            error=BrokerError("PAGE_TIMEOUT", "일부 페이지만 조회됨", retryable=True),
            items=(object(),),
            missing_accounts=("safe-alias",),
        )

        self.assertEqual(_source_status(result), ("PARTIAL", "PAGE_TIMEOUT"))

    def runner(self, repository: FakeRepository) -> ScheduledReportRunner:
        return ScheduledReportRunner(
            settings=make_settings(),
            repository=repository,  # type: ignore[arg-type]
            portfolios=object(),  # type: ignore[arg-type]
            recurring=object(),  # type: ignore[arg-type]
        )

    async def test_same_report_claim_is_not_delivered_twice(self) -> None:
        repository = FakeRepository()
        runner = self.runner(repository)
        context = SimpleNamespace(bot=FakeBot())

        kwargs = {
            "report_type": "daily",
            "period_key": "2026-08-10",
            "user_id": 1001,
            "content": "fixture portfolio report",
        }
        await runner._deliver(context, **kwargs)
        await runner._deliver(context, **kwargs)

        self.assertEqual(repository.report_claim_calls, 2)
        self.assertEqual(len(repository.completed), 1)
        self.assertEqual(len(repository.delivery_ids), 1)
        self.assertEqual(len(context.bot.messages), 1)
        self.assertEqual(
            repository.delivery_marks,
            [{"delivery_id": 101, "status": "SENT"}],
        )

    async def test_uncertain_telegram_send_is_marked_unknown_without_retry(self) -> None:
        repository = FakeRepository()
        runner = self.runner(repository)
        context = SimpleNamespace(bot=FakeBot(fail=True))

        with self.assertLogs("account_bot.scheduler.jobs", level="ERROR") as captured:
            await runner._deliver(
                context,
                report_type="weekly",
                period_key="2026-W33",
                user_id=1001,
                content="fixture weekly report",
            )

        self.assertEqual(len(context.bot.messages), 1)
        self.assertEqual(
            repository.delivery_marks,
            [
                {
                    "delivery_id": 101,
                    "status": "UNKNOWN",
                    "safe_error_type": "RuntimeError",
                }
            ],
        )
        self.assertTrue(any("delivery uncertain" in item for item in captured.output))

    async def test_claimed_report_resumes_before_any_delivery_was_claimed(self) -> None:
        repository = FakeRepository()
        runner = self.runner(repository)
        context = SimpleNamespace(bot=FakeBot())
        audience = fake_derive_fingerprint(
            "1001",
            pepper="fixture-fingerprint-pepper",
            namespace="tg_user",
        )
        idempotency_key = f"daily:2026-08-10:{audience}:v1"
        repository.report_ids[idempotency_key] = 1
        repository.report_states[idempotency_key] = "CLAIMED"

        await runner._deliver(
            context,
            report_type="daily",
            period_key="2026-08-10",
            user_id=1001,
            content="fixture recovered report",
        )

        self.assertEqual(len(repository.completed), 1)
        self.assertEqual(len(context.bot.messages), 1)
        self.assertEqual(repository.delivery_marks, [{"delivery_id": 101, "status": "SENT"}])


class SchedulerRegistrationTests(unittest.TestCase):
    def runner_with(self, settings: Settings) -> ScheduledReportRunner:
        return ScheduledReportRunner(
            settings=settings,
            repository=object(),  # type: ignore[arg-type]
            portfolios=object(),  # type: ignore[arg-type]
            recurring=object(),  # type: ignore[arg-type]
        )

    def test_missing_pepper_or_allowlist_fails_closed_without_jobs(self) -> None:
        cases = {
            "missing_pepper": make_settings(ACCOUNT_BOT_FINGERPRINT_PEPPER=""),
            "missing_allowlist": make_settings(ALLOWED_TELEGRAM_USER_IDS=""),
        }
        for name, settings in cases.items():
            with self.subTest(case=name):
                queue = FakeJobQueue()
                application = SimpleNamespace(job_queue=queue)
                registered = register_scheduled_reports(application, self.runner_with(settings))
                self.assertFalse(registered)
                self.assertEqual(queue.jobs, [])

    def test_weekly_valuation_runs_saturday_before_weekly_report(self) -> None:
        queue = FakeJobQueue()
        application = SimpleNamespace(job_queue=queue)

        registered = register_scheduled_reports(
            application,
            self.runner_with(make_settings()),
        )

        self.assertTrue(registered)
        jobs = {job["name"]: job for job in queue.jobs}
        self.assertEqual(
            set(jobs),
            {
                "account-bot-daily-report",
                "account-bot-weekly-valuation",
                "account-bot-weekly-report",
            },
        )
        valuation = jobs["account-bot-weekly-valuation"]
        self.assertEqual(valuation["days"], (6,))
        self.assertEqual((valuation["time"].hour, valuation["time"].minute), (8, 30))


if __name__ == "__main__":
    unittest.main()
