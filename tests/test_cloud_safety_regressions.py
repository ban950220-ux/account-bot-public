"""Credential-free stdlib regressions for migration and polling ownership."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from account_bot.domain import BrokerResult, Currency, InstrumentKey, Money
from account_bot.polling_guard import require_polling_ownership
from account_bot.portfolio import PortfolioAggregator, RecurringBuyPlan, recommend_recurring_amounts
from account_bot.reports import format_recurring

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "safety_migration_preflight", ROOT / "scripts/cloud/migration_preflight.py")
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)


def synthetic_source(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE broker_accounts (id INTEGER PRIMARY KEY, broker TEXT,
                account_fingerprint TEXT, base_currency TEXT, masked_label TEXT);
            CREATE TABLE instruments (id INTEGER PRIMARY KEY, market TEXT, ticker TEXT,
                asset_type TEXT, currency TEXT);
            CREATE TABLE recurring_buy_plans (id INTEGER PRIMARY KEY, plan_key TEXT,
                instrument_id INTEGER, account_id INTEGER, version INTEGER,
                daily_amount TEXT, currency TEXT, enabled INTEGER, source TEXT,
                target_weight TEXT, timezone_name TEXT, started_at TEXT, memo TEXT);
            CREATE TABLE recurring_buy_revisions (plan_id INTEGER, revision_no INTEGER,
                change_hash TEXT, instrument_id INTEGER, account_id INTEGER,
                daily_amount TEXT, currency TEXT, enabled INTEGER, source TEXT,
                target_weight TEXT, timezone_name TEXT, started_at TEXT,
                effective_from TEXT, memo TEXT);
            CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER,
                source_key TEXT, instrument_id INTEGER, currency TEXT, quantity TEXT,
                status TEXT, transaction_type TEXT, traded_at TEXT, settled_at TEXT,
                unit_price TEXT, gross_amount TEXT, fee TEXT, tax TEXT, net_amount TEXT);
            CREATE TABLE report_deliveries (id INTEGER PRIMARY KEY, status TEXT);
        """)
        db.execute("INSERT INTO broker_accounts VALUES (1,'fixture',?,'KRW',NULL)",
                   ("fixture_" + "a" * 64,))
        db.execute("INSERT INTO instruments VALUES (1,'US','TEST','EQUITY','USD')")
        db.execute("""INSERT INTO recurring_buy_plans VALUES
            (1,'fixture',1,1,1,'2000.25','KRW',1,'MANUAL',NULL,
             'Asia/Seoul','2026-01-01T00:00:00Z',NULL)""")
        db.execute("""INSERT INTO recurring_buy_revisions VALUES
            (1,1,?,1,1,'2000.25','KRW',1,'MANUAL',NULL,'Asia/Seoul',
             '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL)""", ("b" * 64,))
        db.execute("""INSERT INTO transactions VALUES
            (1,1,'fixture-trade',1,'USD','0.000000000000000001','FILLED','BUY',
             '2026-01-01T00:00:00Z',NULL,'0.1',NULL,NULL,NULL,NULL)""")


class MigrationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.backup = self.root / "fixture.db", self.root / "backup.db"
        synthetic_source(self.source)

    def audit(self):
        return migration.backup_and_audit(self.source, self.backup)

    def test_nonfinite_and_exponent_money_block_active_import(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity", "1e3", "1e999999999"):
            with self.subTest(raw=raw):
                with closing(sqlite3.connect(self.source)) as db, db:
                    db.execute("UPDATE transactions SET quantity=?", (raw,))
                self.backup.unlink(missing_ok=True)
                audit = self.audit()
                self.assertIn("invalid_amount:transactions:quantity", audit.blockers)
                self.assertFalse(audit.can_import)

    def test_required_missing_quantity_is_not_a_zero_or_string_none(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE transactions SET quantity=NULL")
        audit = self.audit()
        self.assertIn("missing_amount:transactions:quantity", audit.blockers)
        self.assertEqual(audit.totals["transaction_gross:missing"], "1")

    def test_totals_preserve_more_than_28_significant_digits(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE transactions SET gross_amount=?",
                       ("123456789012345678901234567890.123456789012345678",))
            db.execute("""INSERT INTO transactions SELECT 2,account_id,'second',instrument_id,
                currency,quantity,status,transaction_type,traded_at,settled_at,
                unit_price,'0.000000000000000001',fee,tax,net_amount FROM transactions""")
        audit = self.audit()
        self.assertTrue(audit.can_import, audit.blockers)
        self.assertEqual(audit.totals["transaction_gross:USD"],
                         "123456789012345678901234567890.123456789012345679")
        migration.import_supported_test_subset(self.backup, audit, self.root / "active-test.db",
                                               ROOT / "deploy/cloudflare/schema.sql")

    def test_current_plan_and_last_revision_must_match(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE recurring_buy_plans SET daily_amount='3000'")
        audit = self.audit()
        self.assertIn("plan_revision_head_mismatch", audit.blockers)
        with self.assertRaises(migration.MigrationBlocked):
            migration.import_supported_test_subset(self.backup, audit, self.root / "blocked.db",
                                                   ROOT / "deploy/cloudflare/schema.sql")
        self.assertFalse((self.root / "blocked.db").exists())

    def test_revision_order_is_checked_even_with_matching_count(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE recurring_buy_revisions SET revision_no=2")
        self.assertIn("plan_revision_order_mismatch", self.audit().blockers)

    def test_semantically_equal_decimal_and_timestamp_do_not_conflict(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("""UPDATE recurring_buy_revisions SET daily_amount='2000.2500',
                started_at='2026-01-01 00:00:00'""")
        self.assertTrue(self.audit().can_import)

    def test_wal_backup_archive_and_rollback_preserve_unknown_and_schema(self) -> None:
        with closing(sqlite3.connect(self.source)) as live, live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("INSERT INTO report_deliveries VALUES (1,'UNKNOWN')")
            live.execute("CREATE INDEX fixture_status ON report_deliveries(status)")
            live.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
            live.executemany("INSERT INTO schema_migrations VALUES (?)", [(1,), (2,)])
            live.commit()
            audit = self.audit()
            self.assertFalse(audit.can_import)  # Legacy delivery must stay inert.
            target, restored = self.root / "archive.db", self.root / "restored.db"
            migration.archive_legacy_test_db(self.backup, audit, target,
                                              ROOT / "deploy/cloudflare/schema.sql")
            migration.restore_legacy_test_db(target, restored)
            with closing(sqlite3.connect(restored)) as db, db, closing(sqlite3.connect(target)) as archive, archive:
                self.assertEqual(migration._logical_digest(db), audit.logical_sha256)
                self.assertEqual(db.execute("SELECT status FROM report_deliveries").fetchone(),
                                 ("UNKNOWN",))
                self.assertEqual(db.execute("SELECT version FROM schema_migrations "
                                            "ORDER BY version").fetchall(), [(1,), (2,)])
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE "
                                               "type='index' AND name='fixture_status'").fetchone())
                for table in ("cloud_outbox", "cloud_update_claims", "cloud_report_claims"):
                    self.assertEqual(archive.execute(f"SELECT count(*) FROM {table}").fetchone(),
                                     (0,))

    def test_historical_orphan_rolls_back_late_subset_import(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE recurring_buy_plans SET version=2")
            db.execute("""INSERT INTO recurring_buy_revisions SELECT plan_id,2,?,instrument_id,
                account_id,daily_amount,currency,enabled,source,target_weight,timezone_name,
                started_at,'2026-01-02T00:00:00Z',memo FROM recurring_buy_revisions""",
                       ("c" * 64,))
            db.execute("UPDATE recurring_buy_revisions SET instrument_id=999 WHERE revision_no=1")
        audit = self.audit()
        self.assertTrue(audit.can_import)
        target = self.root / "rolled-back.db"
        with self.assertRaisesRegex(migration.MigrationBlocked, "revision reference"):
            migration.import_supported_test_subset(self.backup, audit, target,
                                                   ROOT / "deploy/cloudflare/schema.sql")
        with closing(sqlite3.connect(target)) as db, db:
            for table in ("cloud_accounts", "cloud_plans", "cloud_plan_revisions"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone(), (0,))

    def test_python_bundle_through_mock_d1_keeps_history_inert(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO report_deliveries VALUES (1,'UNKNOWN')")
        audit = self.audit()
        bundle = migration.build_d1_archive_bundle(self.backup, audit, max_items=10)
        path = self.root / "synthetic-bundle.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(["node", str(ROOT / "tests/cloud_js_archive_runner.mjs"), str(path)],
                                capture_output=True, encoding="utf-8", timeout=20, check=True)
        self.assertEqual(json.loads(result.stdout),
                         {"status": "VERIFIED", "count": bundle["manifest"]["rowCount"]})

    def test_legacy_restore_blocks_instead_of_discarding_new_cloud_state(self) -> None:
        audit = self.audit()
        target, restored = self.root / "archive.db", self.root / "restored.db"
        migration.archive_legacy_test_db(self.backup, audit, target,
                                          ROOT / "deploy/cloudflare/schema.sql")
        with closing(sqlite3.connect(target)) as db, db:
            db.execute("""INSERT INTO cloud_outbox
                (event_key,destination_key,payload_json,status,created_at)
                VALUES ('synthetic','42','{}','UNKNOWN','2026-01-01T00:00:00Z')""")
        with self.assertRaisesRegex(migration.MigrationBlocked, "reconciled rollback"):
            migration.restore_legacy_test_db(target, restored)
        self.assertFalse(restored.exists())
        self.assertFalse(restored.with_suffix(".db.partial").exists())
        with closing(sqlite3.connect(target)) as db, db:
            self.assertEqual(db.execute("SELECT status FROM cloud_outbox").fetchone(), ("UNKNOWN",))

    def test_binary_float_cannot_be_promoted_as_exact_money(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='transactions'").fetchone()[0]
            db.execute("ALTER TABLE transactions RENAME TO old_transactions")
            db.execute(sql.replace("quantity TEXT", "quantity"))
            db.execute("INSERT INTO transactions SELECT * FROM old_transactions")
            db.execute("DROP TABLE old_transactions")
            db.execute("UPDATE transactions SET quantity=0.125")
        self.assertIn("imprecise_sqlite_real:transactions", self.audit().blockers)


class PollingGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_webhook_is_never_removed(self) -> None:
        bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=
                              SimpleNamespace(url="https://fixture.invalid/webhook")),
                              delete_webhook=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "webhook present"):
            await require_polling_ownership(bot)
        bot.delete_webhook.assert_not_called()

    async def test_unknown_or_failed_lookup_is_fail_closed_and_redacted(self) -> None:
        for result in (None, SimpleNamespace(), SimpleNamespace(url=None)):
            bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=result))
            with self.assertRaises(RuntimeError):
                await require_polling_ownership(bot)
        bot = SimpleNamespace(get_webhook_info=AsyncMock(side_effect=OSError("secret-fixture")))
        with self.assertRaisesRegex(RuntimeError, "could not be verified") as error:
            await require_polling_ownership(bot)
        self.assertNotIn("secret-fixture", str(error.exception))

    async def test_only_confirmed_empty_webhook_allows_polling(self) -> None:
        bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")))
        await require_polling_ownership(bot)
        bot.get_webhook_info.assert_awaited_once()


class RecurringThirteenFixtureTests(unittest.TestCase):
    def test_declared_thirteen_schedule_totals_and_python_js_parity(self) -> None:
        # User-declared amounts only; no account, file, broker, or operating DB read.
        amounts = {ticker: "2000" for ticker in
                   "ROK JCI VRT GEV CGNX CAT TXN ETN CRWD PANW".split()}
        amounts.update(TT="1500", ECL="1500", **{"PBR.A": "1000"})
        plans = tuple(RecurringBuyPlan(InstrumentKey("US", ticker), Money(amount, "KRW"),
                                       broker="kiwoom_accum")
                      for ticker, amount in amounts.items())
        self.assertEqual(sum(plan.daily_amount.amount for plan in plans), Decimal("24000"))
        recommendations, summaries = recommend_recurring_amounts(plans, {})
        self.assertEqual(summaries[0].current_weekly_amount, Decimal("120000"))
        self.assertEqual(summaries[0].recommended_weekly_amount, Decimal("120000"))
        self.assertTrue(all(item.recommended_amount == item.plan.daily_amount.amount
                            for item in recommendations))
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(
            (BrokerResult.ok("fixture"),), expected_brokers=("fixture",))
        report = format_recurring(plans, snapshot)
        self.assertIn("월평균 환산 520,000원", report)
        self.assertIn("산업재 47.9% ⚠️", report)
        self.assertIn("분류 확인 불가 20.8%", report)
        for day in "월화수목금":
            self.assertIn(f"{day} 24,000원", report)
        js_plans = [{"instrument": {"market": "US", "ticker": ticker,
                     "assetType": "EQUITY", "currency": "USD"},
                     "dailyAmount": amount, "currency": "KRW", "enabled": True,
                     "broker": "kiwoom_accum", "scheduledWeekdays": [0, 1, 2, 3, 4]}
                    for ticker, amount in amounts.items()]
        result = subprocess.run(["node", "--input-type=module", "-e",
            "import {readFileSync} from 'node:fs'; "
            "import {formatRecurring} from './deploy/cloudflare-js/recurring-report.mjs'; "
            "process.stdout.write(formatRecurring(JSON.parse(readFileSync(0,'utf8'))));"],
            cwd=ROOT, input=json.dumps(js_plans), capture_output=True, encoding="utf-8",
            timeout=20, check=True)
        self.assertEqual(result.stdout, report)
