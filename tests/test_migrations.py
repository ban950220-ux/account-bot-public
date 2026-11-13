from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.storage import (  # noqa: E402
    LATEST_SCHEMA_VERSION,
    Database,
    SchemaMigrationRequiredError,
)
from account_bot.storage.migrations import (  # noqa: E402
    IMPORT_TABLE_NAMES,
    LEGACY_TABLE_NAMES,
    SCENARIO_TABLE_NAMES,
    current_schema_version,
    rollback_latest_for_tests,
)
from account_bot.storage.models import Base  # noqa: E402


async def table_names(database: Database) -> set[str]:
    async with database.engine.connect() as connection:
        return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


async def create_legacy_schema(database: Database) -> None:
    legacy_tables = [Base.metadata.tables[name] for name in sorted(LEGACY_TABLE_NAMES)]
    async with database.engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=legacy_tables)
        )
        await connection.execute(
            text(
                "INSERT INTO portfolios "
                "(key, name, base_currency, timezone_name, enabled, created_at, updated_at) "
                "VALUES "
                "('migration-sentinel', 'Migration Sentinel', 'KRW', 'Asia/Seoul', 1, "
                "'2026-09-11 00:00:00', '2026-09-11 00:00:00')"
            )
        )


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    def database(self, name: str) -> Database:
        path = self.root / name
        return Database(f"sqlite+aiosqlite:///{path.as_posix()}")

    async def test_case_a_empty_database_reaches_latest_schema(self) -> None:
        database = self.database("empty.db")
        try:
            await database.initialize()
            names = await table_names(database)
            async with database.engine.connect() as connection:
                version = await current_schema_version(connection)
        finally:
            await database.dispose()

        self.assertEqual(version, LATEST_SCHEMA_VERSION)
        self.assertTrue(LEGACY_TABLE_NAMES.issubset(names))
        self.assertTrue(set(IMPORT_TABLE_NAMES).issubset(names))
        self.assertTrue(set(SCENARIO_TABLE_NAMES).issubset(names))
        self.assertIn("schema_migrations", names)

    async def test_normal_startup_refuses_unversioned_legacy_database(self) -> None:
        database = self.database("startup-refusal.db")
        await create_legacy_schema(database)
        await database.dispose()

        reopened = self.database("startup-refusal.db")
        try:
            with self.assertRaises(SchemaMigrationRequiredError):
                await reopened.initialize()
            names = await table_names(reopened)
        finally:
            await reopened.dispose()

        self.assertNotIn("schema_migrations", names)
        self.assertTrue(set(IMPORT_TABLE_NAMES).isdisjoint(names))

    async def test_case_b_legacy_upgrade_creates_verified_backup_and_preserves_data(self) -> None:
        database_path = self.root / "legacy.db"
        database = self.database("legacy.db")
        await create_legacy_schema(database)
        await database.dispose()

        migrator = self.database("legacy.db")
        try:
            report = await migrator.migrate_to_latest(
                backup_directory=self.root / "backups"
            )
            names = await table_names(migrator)
            async with migrator.engine.connect() as connection:
                sentinel = await connection.scalar(
                    text("SELECT name FROM portfolios WHERE key='migration-sentinel'")
                )
                version = await current_schema_version(connection)
        finally:
            await migrator.dispose()

        self.assertEqual(report.previous_version, 0)
        self.assertEqual(report.current_version, LATEST_SCHEMA_VERSION)
        self.assertEqual(report.applied_versions, (1, 2))
        self.assertIsNotNone(report.backup)
        assert report.backup is not None
        self.assertTrue(report.backup.path.is_file())
        self.assertGreater(report.backup.size, 0)
        self.assertEqual(len(report.backup.sha256), 64)
        self.assertNotEqual(report.backup.path.resolve(), database_path.resolve())
        self.assertEqual(version, LATEST_SCHEMA_VERSION)
        self.assertEqual(sentinel, "Migration Sentinel")
        self.assertTrue(set(IMPORT_TABLE_NAMES).issubset(names))
        self.assertTrue(set(SCENARIO_TABLE_NAMES).issubset(names))

        backup = sqlite3.connect(
            f"file:{report.backup.path.as_posix()}?mode=ro", uri=True
        )
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            backed_up_sentinel = backup.execute(
                "SELECT name FROM portfolios WHERE key='migration-sentinel'"
            ).fetchone()
            backup_tables = {
                row[0]
                for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            backup.close()
        self.assertEqual(integrity, ("ok",))
        self.assertEqual(backed_up_sentinel, ("Migration Sentinel",))
        self.assertNotIn("schema_migrations", backup_tables)

    async def test_version_one_database_upgrades_to_scenarios_with_backup(self) -> None:
        database = self.database("version-one.db")
        await database.initialize()
        async with database.engine.begin() as connection:
            self.assertEqual(await rollback_latest_for_tests(connection), 1)
        await database.dispose()

        migrator = self.database("version-one.db")
        try:
            report = await migrator.migrate_to_latest(
                backup_directory=self.root / "v1-backups"
            )
            names = await table_names(migrator)
        finally:
            await migrator.dispose()

        self.assertEqual(report.previous_version, 1)
        self.assertEqual(report.applied_versions, (2,))
        self.assertTrue(set(SCENARIO_TABLE_NAMES).issubset(names))
        assert report.backup is not None
        backup = sqlite3.connect(
            f"file:{report.backup.path.as_posix()}?mode=ro", uri=True
        )
        try:
            backup_version = backup.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
        finally:
            backup.close()
        self.assertEqual(backup_version, (1,))

    async def test_case_c_latest_migration_rolls_back_without_touching_legacy_data(self) -> None:
        database = self.database("rollback.db")
        await create_legacy_schema(database)
        await database.dispose()

        migrated = self.database("rollback.db")
        try:
            await migrated.migrate_to_latest(backup_directory=self.root / "backups")
            async with migrated.engine.begin() as connection:
                first_version = await rollback_latest_for_tests(connection)
            names_after_latest = await table_names(migrated)
            async with migrated.engine.begin() as connection:
                version = await rollback_latest_for_tests(connection)
            names = await table_names(migrated)
            async with migrated.engine.connect() as connection:
                sentinel = await connection.scalar(
                    text("SELECT name FROM portfolios WHERE key='migration-sentinel'")
                )
                stored_version = await current_schema_version(connection)
        finally:
            await migrated.dispose()

        self.assertEqual(first_version, 1)
        self.assertTrue(set(IMPORT_TABLE_NAMES).issubset(names_after_latest))
        self.assertTrue(set(SCENARIO_TABLE_NAMES).isdisjoint(names_after_latest))
        self.assertEqual(version, 0)
        self.assertEqual(stored_version, 0)
        self.assertEqual(sentinel, "Migration Sentinel")
        self.assertTrue(set(IMPORT_TABLE_NAMES).isdisjoint(names))
        self.assertIn("schema_migrations", names)


if __name__ == "__main__":
    unittest.main()
