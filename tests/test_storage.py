from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    from sqlalchemy import func, select, text

    from account_bot.domain import Currency
    from account_bot.services.storage_adapters import SqlAlchemyRecurringPlanGateway
    from account_bot.storage import (
        CashSnapshot,
        Database,
        DatabaseConfigurationError,
        Instrument,
        PortfolioSnapshot,
        PositionSnapshot,
        RecurringBuyRevision,
        Report,
        RepositoryValidationError,
        SnapshotSource,
        SQLitePortfolioRepository,
        ThesisAssessment,
        ThesisRevision,
        derive_fingerprint,
    )

    STORAGE_AVAILABLE = True
except ModuleNotFoundError:
    STORAGE_AVAILABLE = False


@unittest.skipUnless(STORAGE_AVAILABLE, "SQLAlchemy/aiosqlite are not installed")
class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_directory.name) / "account-bot.db"
        self.database = Database(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            sqlite_busy_timeout_ms=7_000,
        )
        await self.database.initialize()
        self.repository = SQLitePortfolioRepository(self.database)
        await self.repository.initialize_default_portfolio()
        self.now = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.database.dispose()
        self.temp_directory.cleanup()

    async def test_sqlite_safety_pragmas_are_enabled(self) -> None:
        async with self.database.engine.connect() as connection:
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
            journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
            busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(busy_timeout, 7_000)

    async def test_malformed_database_url_does_not_echo_input(self) -> None:
        with self.assertRaises(DatabaseConfigurationError) as caught:
            Database("malformed-secret-bearing-url")
        self.assertNotIn("malformed-secret-bearing-url", str(caught.exception))

    async def test_account_storage_requires_fingerprint_not_raw_identifier(self) -> None:
        fingerprint = derive_fingerprint(
            "raw-account-1234", pepper="test-only-pepper", namespace="account"
        )
        account = await self.repository.upsert_account(
            broker="kiwoom",
            account_fingerprint=fingerprint,
            base_currency="KRW",
            masked_label="primary",
        )
        self.assertEqual(account.account_fingerprint, fingerprint)
        with self.assertRaises(RepositoryValidationError):
            await self.repository.upsert_account(
                broker="kiwoom",
                account_fingerprint="1234567890",
                base_currency="KRW",
            )

    async def test_instrument_identity_keeps_asset_type_and_currency_distinct(self) -> None:
        account = await self.repository.upsert_account(
            broker="kiwoom",
            account_fingerprint=derive_fingerprint(
                "identity-test-account",
                pepper="test-only-pepper",
                namespace="account",
            ),
            base_currency="USD",
        )
        claim = await self.repository.claim_snapshot(
            idempotency_key="instrument-identity:2026-08-09T06:00Z",
            captured_at=self.now,
            data_as_of=self.now,
        )
        repeated_claim = await self.repository.claim_snapshot(
            idempotency_key="instrument-identity:2026-08-09T06:00Z",
            captured_at=self.now,
            data_as_of=self.now,
        )
        self.assertFalse(repeated_claim.created)
        self.assertEqual(repeated_claim.record_id, claim.record_id)
        self.assertEqual(repeated_claim.state, "COLLECTING")
        variants = (("EQUITY", "USD"), ("ETF", "USD"), ("EQUITY", "KRW"))
        for asset_type, currency in variants:
            await self.repository.add_position(
                snapshot_id=claim.record_id,
                account_id=account.id,
                market="NASDAQ",
                ticker="SAME",
                asset_type=asset_type,
                currency=currency,
                quantity="1",
                market_value="10",
            )

        async with self.database.session() as session:
            instruments = list(
                (
                    await session.execute(
                        select(Instrument).where(
                            Instrument.market == "NASDAQ",
                            Instrument.ticker == "SAME",
                        )
                    )
                ).scalars()
            )
            position_count = (
                await session.execute(
                    select(func.count())
                    .select_from(PositionSnapshot)
                    .where(PositionSnapshot.snapshot_id == claim.record_id)
                )
            ).scalar_one()

        self.assertEqual(
            {(item.asset_type, item.currency) for item in instruments},
            set(variants),
        )
        self.assertEqual(position_count, 3)

    async def test_collecting_snapshot_resume_discards_incomplete_child_generation(self) -> None:
        account = await self.repository.upsert_account(
            broker="kiwoom",
            account_fingerprint=derive_fingerprint(
                "resume-test-account",
                pepper="test-only-pepper",
                namespace="account",
            ),
            base_currency="KRW",
        )
        key = "resume-snapshot:2026-08-09"
        first = await self.repository.claim_snapshot(
            idempotency_key=key,
            captured_at=self.now,
            data_as_of=self.now,
        )
        await self.repository.record_snapshot_source(
            snapshot_id=first.record_id,
            source_key="kiwoom:positions",
            status="SUCCESS",
            observed_at=self.now,
        )
        await self.repository.add_position(
            snapshot_id=first.record_id,
            account_id=account.id,
            market="KOSPI",
            ticker="005930",
            currency="KRW",
            quantity="1",
            market_value="100",
        )
        await self.repository.add_cash(
            snapshot_id=first.record_id,
            account_id=account.id,
            currency="KRW",
            amount="50",
        )

        resumed_at = self.now + timedelta(minutes=5)
        resumed = await self.repository.claim_snapshot(
            idempotency_key=key,
            captured_at=resumed_at,
            data_as_of=resumed_at,
        )

        self.assertFalse(resumed.created)
        self.assertEqual(resumed.state, "COLLECTING")
        async with self.database.session() as session:
            child_counts = []
            for model in (SnapshotSource, PositionSnapshot, CashSnapshot):
                child_counts.append(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(model)
                            .where(model.snapshot_id == first.record_id)
                        )
                    ).scalar_one()
                )
            snapshot = await session.get(PortfolioSnapshot, first.record_id)

        self.assertEqual(child_counts, [0, 0, 0])
        self.assertEqual(snapshot.captured_at, resumed_at)
        self.assertEqual(snapshot.data_as_of, resumed_at)

    async def test_recurring_plan_is_idempotent_and_occurrence_captures_revision(self) -> None:
        plan = await self.repository.upsert_recurring_buy(
            plan_key="daily:nasdaq:amd",
            market="NASDAQ",
            ticker="AMD",
            daily_amount="20",
            currency="USD",
            target_weight="0.08",
            started_at=self.now,
            effective_at=self.now,
        )
        repeated = await self.repository.upsert_recurring_buy(
            plan_key="daily:nasdaq:amd",
            market="NASDAQ",
            ticker="AMD",
            daily_amount="20.00000000",
            currency="USD",
            target_weight="0.0800000000",
            effective_at=self.now + timedelta(minutes=1),
        )
        self.assertEqual(plan.id, repeated.id)
        self.assertEqual(repeated.version, 1)
        view = await self.repository.get_recurring_buy_view("daily:nasdaq:amd")
        self.assertIsNotNone(view)
        self.assertEqual((view.market, view.ticker), ("NASDAQ", "AMD"))

        occurrence = await self.repository.ensure_recurring_occurrence(
            plan_id=plan.id, local_due_date=date(2026, 8, 10)
        )
        self.assertEqual(occurrence.planned_amount, Decimal("20.00000000"))
        async with self.database.session() as session:
            first_revision = (
                await session.execute(
                    select(RecurringBuyRevision).where(
                        RecurringBuyRevision.plan_id == plan.id,
                        RecurringBuyRevision.revision_no == 1,
                    )
                )
            ).scalar_one()
        self.assertEqual(occurrence.plan_revision_id, first_revision.id)
        self.assertEqual(first_revision.instrument_id, plan.instrument_id)

        changed = await self.repository.upsert_recurring_buy(
            plan_key="daily:nasdaq:amd",
            market="NASDAQ",
            ticker="AMD",
            daily_amount="25",
            currency="USD",
            target_weight="0.08",
            started_at=self.now,
            effective_at=self.now + timedelta(days=1),
        )
        self.assertEqual(changed.version, 2)
        async with self.database.session() as session:
            revisions = (
                await session.execute(select(func.count()).select_from(RecurringBuyRevision))
            ).scalar_one()
        self.assertEqual(revisions, 2)
        self.assertEqual(occurrence.planned_amount, Decimal("20.00000000"))

        disabled = await self.repository.set_recurring_buy_enabled(
            plan_key="daily:nasdaq:amd",
            enabled=False,
            effective_at=self.now + timedelta(days=2),
        )
        self.assertFalse(disabled.enabled)
        restored = await self.repository.set_recurring_buy_enabled(
            plan_key="daily:nasdaq:amd",
            enabled=True,
            effective_at=self.now + timedelta(days=3),
        )
        self.assertTrue(restored.enabled)
        self.assertEqual(restored.version, 4)

    async def test_instrument_metadata_can_upgrade_equity_to_etf_without_duplicate(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                text(
                    "CREATE UNIQUE INDEX legacy_instrument_identity "
                    "ON instruments(market, ticker)"
                )
            )
        first = await self.repository.upsert_recurring_buy(
            plan_key="daily:us:qqq",
            market="US",
            ticker="QQQ",
            daily_amount="2000",
            currency="KRW",
            instrument_currency="USD",
        )
        upgraded = await self.repository.upsert_recurring_buy(
            plan_key="daily:us:qqq",
            market="US",
            ticker="QQQ",
            daily_amount="2000",
            currency="KRW",
            instrument_currency="USD",
            asset_type="ETF",
        )
        view = await self.repository.get_recurring_buy_view("daily:us:qqq")

        self.assertEqual(first.instrument_id, upgraded.instrument_id)
        self.assertIsNotNone(view)
        self.assertEqual(view.asset_type, "ETF")

    async def test_recurring_gateway_repeat_keeps_original_start_and_revision(self) -> None:
        gateway = SqlAlchemyRecurringPlanGateway(self.repository)
        first = await gateway.upsert_plan(
            market="NASDAQ",
            ticker="AMD",
            amount=Decimal("20"),
            currency=Currency.USD,
            target_weight_pct=Decimal("8"),
        )
        second = await gateway.upsert_plan(
            market="NASDAQ",
            ticker="AMD",
            amount=Decimal("20"),
            currency=Currency.USD,
            target_weight_pct=Decimal("8"),
        )
        stored = await self.repository.get_recurring_buy("manual:NASDAQ:AMD")
        async with self.database.session() as session:
            revision_count = (
                await session.execute(select(func.count()).select_from(RecurringBuyRevision))
            ).scalar_one()

        self.assertEqual(first.started_at, second.started_at)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.version, 1)
        self.assertEqual(revision_count, 1)

    async def test_recurring_gateway_preserves_krw_funding_and_weekly_schedule(self) -> None:
        await self.repository.upsert_recurring_buy(
            plan_key="kiwoom-photo:US:CME",
            market="US",
            ticker="CME",
            daily_amount="2000",
            currency="KRW",
            instrument_currency="USD",
            source="KIWOOM_SCREENSHOT",
            memo="[schedule=MON] 키움 모으기 설정",
            started_at=self.now,
        )
        gateway = SqlAlchemyRecurringPlanGateway(self.repository)

        plans = await gateway.list_plans(enabled_only=True)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].daily_amount.currency, Currency.KRW)
        self.assertEqual(plans[0].instrument.currency, Currency.USD)
        self.assertEqual(plans[0].scheduled_weekdays, (0,))

    async def test_thesis_is_append_only_and_revision_key_is_idempotent(self) -> None:
        first = await self.repository.set_investment_thesis(
            revision_key="amd-initial",
            market="NASDAQ",
            ticker="AMD",
            currency="USD",
            thesis_text="Data-center share gains and accelerator growth.",
            effective_at=self.now,
        )
        repeated = await self.repository.set_investment_thesis(
            revision_key="amd-initial",
            market="NASDAQ",
            ticker="AMD",
            currency="USD",
            thesis_text="Different text must not overwrite an idempotent revision.",
            effective_at=self.now + timedelta(hours=1),
        )
        second = await self.repository.set_investment_thesis(
            revision_key="amd-q3-review",
            market="NASDAQ",
            ticker="AMD",
            currency="USD",
            thesis_text="The thesis remains intact after the quarterly review.",
            effective_at=self.now + timedelta(days=1),
        )
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(second.revision_no, 2)
        current = await self.repository.get_current_investment_thesis(market="NASDAQ", ticker="AMD")
        self.assertIsNotNone(current)
        self.assertEqual(current.id, second.id)
        async with self.database.session() as session:
            count = (
                await session.execute(select(func.count()).select_from(ThesisRevision))
            ).scalar_one()
        self.assertEqual(count, 2)

        history = await self.repository.list_investment_thesis_history(
            market="NASDAQ", ticker="AMD"
        )
        self.assertEqual([revision.id for revision in history], [first.id, second.id])

        assessment = await self.repository.record_thesis_assessment(
            thesis_revision_id=second.id,
            assessment_key="amd-q3-input",
            analyzer_version="rules-v1",
            input_hash="b" * 64,
            verdict="MAINTAINED",
            rationale="Quarterly evidence remains consistent with the current thesis.",
            assessed_at=self.now + timedelta(days=1),
        )
        repeated_assessment = await self.repository.record_thesis_assessment(
            thesis_revision_id=second.id,
            assessment_key="amd-q3-input",
            analyzer_version="rules-v1",
            input_hash="b" * 64,
            verdict="BROKEN",
            rationale="An idempotent retry must not replace the stored assessment.",
            assessed_at=self.now + timedelta(days=2),
        )
        self.assertEqual(assessment.id, repeated_assessment.id)
        self.assertEqual(repeated_assessment.verdict, "MAINTAINED")
        async with self.database.session() as session:
            assessment_count = (
                await session.execute(select(func.count()).select_from(ThesisAssessment))
            ).scalar_one()
        self.assertEqual(assessment_count, 1)

    async def test_cache_rejects_secrets_and_failure_keeps_last_good_value(self) -> None:
        entry = await self.repository.put_market_cache(
            provider="provider-a",
            namespace="price",
            cache_key="NASDAQ:AMD",
            normalized_data={"price": Decimal("168.20"), "currency": "USD"},
            fetched_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            stale_until=self.now + timedelta(days=1),
        )
        self.assertEqual(entry.normalized_data["price"], "168.20")

        await self.repository.put_market_cache(
            provider="provider-a",
            namespace="price",
            cache_key="NASDAQ:AMD",
            normalized_data={"price": "1.00", "currency": "USD"},
            fetched_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=1),
            stale_until=self.now + timedelta(hours=1),
        )
        await self.repository.record_market_fetch_failure(
            provider="provider-a",
            namespace="price",
            cache_key="NASDAQ:AMD",
            attempted_at=self.now + timedelta(minutes=10),
            safe_error_type="ProviderTimeout",
        )
        stale = await self.repository.get_cache_with_stale_fallback(
            provider="provider-a",
            namespace="price",
            cache_key="NASDAQ:AMD",
            now=self.now + timedelta(minutes=10),
        )
        self.assertIsNotNone(stale)
        self.assertTrue(stale.is_stale)
        self.assertEqual(stale.data["price"], "168.20")

        with self.assertRaises(RepositoryValidationError):
            await self.repository.put_market_cache(
                provider="provider-a",
                namespace="price",
                cache_key="unsafe",
                normalized_data={"access_token": "must-not-persist"},
                fetched_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )

    async def test_fx_observation_and_last_good_lookup_are_idempotent(self) -> None:
        first = await self.repository.add_fx_rate(
            observation_key="USD-KRW:2026-08-09T06:00Z",
            provider="provider-a",
            base_currency="USD",
            quote_currency="KRW",
            rate="1380.25",
            effective_at=self.now,
        )
        repeated = await self.repository.add_fx_rate(
            observation_key="USD-KRW:2026-08-09T06:00Z",
            provider="provider-a",
            base_currency="USD",
            quote_currency="KRW",
            rate="9999",
            effective_at=self.now,
        )
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(repeated.rate, Decimal("1380.250000000000"))
        lookup = await self.repository.get_latest_fx_rate(
            base_currency="USD",
            quote_currency="KRW",
            now=self.now + timedelta(hours=1),
            max_age=timedelta(days=1),
        )
        self.assertIsNotNone(lookup)
        self.assertEqual(lookup.rate, Decimal("1380.250000000000"))

    async def test_saved_scenario_round_trips_exact_decimal_and_replaces_by_name(self) -> None:
        first = await self.repository.save_investment_scenario(
            name="QQQ 강화",
            items=[
                {
                    "market": "US",
                    "ticker": "QQQ",
                    "asset_type": "ETF",
                    "instrument_currency": "USD",
                    "configured_amount_krw": Decimal("10000.125"),
                    "enabled": True,
                }
            ],
        )
        repeated = await self.repository.save_investment_scenario(
            name="QQQ 강화",
            items=[
                {
                    "market": "US",
                    "ticker": "QQQ",
                    "asset_type": "ETF",
                    "instrument_currency": "USD",
                    "configured_amount_krw": Decimal("9000.875"),
                    "enabled": False,
                }
            ],
        )
        self.assertEqual(first.scenario_id, repeated.scenario_id)
        self.assertEqual(repeated.items[0].configured_amount_krw, Decimal("9000.875"))
        self.assertFalse(repeated.items[0].enabled)
        scenarios = await self.repository.list_investment_scenarios()
        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0].name, "QQQ 강화")
        self.assertTrue(
            await self.repository.delete_investment_scenario(first.scenario_id)
        )
        self.assertEqual(await self.repository.list_investment_scenarios(), [])
        with self.assertRaisesRegex(
            RepositoryValidationError, "must not use binary float"
        ):
            await self.repository.save_investment_scenario(
                name="잘못된 숫자",
                items=[
                    {
                        "market": "US",
                        "ticker": "QQQ",
                        "asset_type": "ETF",
                        "instrument_currency": "USD",
                        "configured_amount_krw": 3000.5,
                        "enabled": True,
                    }
                ],
            )

    async def test_report_claim_and_delivery_are_idempotent_without_body_storage(self) -> None:
        audience = derive_fingerprint(
            "telegram-user-id", pepper="test-only-pepper", namespace="audience"
        )
        first = await self.repository.claim_report(
            idempotency_key="daily:default:2026-08-10:v1",
            report_type="DAILY",
            period_key="2026-08-10",
            audience_fingerprint=audience,
            claimed_at=self.now,
        )
        repeated = await self.repository.claim_report(
            idempotency_key="daily:default:2026-08-10:v1",
            report_type="DAILY",
            period_key="2026-08-10",
            audience_fingerprint=audience,
            claimed_at=self.now + timedelta(minutes=1),
        )
        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(first.record_id, repeated.record_id)

        body = "Sensitive portfolio report body that must not be stored"
        report = await self.repository.complete_report(
            report_id=first.record_id,
            input_hash="a" * 64,
            content=body,
            completed_at=self.now + timedelta(minutes=2),
        )
        self.assertEqual(report.status, "READY")
        self.assertFalse(hasattr(Report, "content"))
        async with self.database.session() as session:
            stored = await session.get(Report, first.record_id)
            self.assertNotIn(body, repr(stored.__dict__))

    async def test_failed_source_cannot_finalize_as_complete(self) -> None:
        claim = await self.repository.claim_snapshot(
            idempotency_key="portfolio:2026-08-09T06:00Z",
            captured_at=self.now,
            data_as_of=self.now,
        )
        await self.repository.record_snapshot_source(
            snapshot_id=claim.record_id,
            source_key="KIWOOM",
            status="SUCCESS",
            observed_at=self.now,
        )
        await self.repository.record_snapshot_source(
            snapshot_id=claim.record_id,
            source_key="TOSS",
            status="FAILED",
            safe_error_type="ProviderTimeout",
        )
        with self.assertRaises(RepositoryValidationError):
            await self.repository.finalize_snapshot(
                snapshot_id=claim.record_id,
                status="COMPLETE",
                total_market_value="100",
            )
        partial = await self.repository.finalize_snapshot(
            snapshot_id=claim.record_id,
            status="PARTIAL",
            total_market_value="100",
        )
        self.assertEqual(partial.status, "PARTIAL")


if __name__ == "__main__":
    unittest.main()
