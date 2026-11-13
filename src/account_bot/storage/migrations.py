"""Versioned, backup-first SQLite schema migrations.

Normal application startup never upgrades an existing database.  A database
with pending migrations must be upgraded through the explicit migration path,
which creates and verifies an online SQLite backup before any DDL is applied.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from .models import Base, SchemaMigration

LEGACY_TABLE_NAMES = frozenset(
    {
        "portfolios",
        "broker_accounts",
        "instruments",
        "fx_rate_observations",
        "portfolio_snapshots",
        "snapshot_sources",
        "position_snapshots",
        "cash_snapshots",
        "transactions",
        "recurring_buy_plans",
        "recurring_buy_revisions",
        "recurring_buy_occurrences",
        "target_allocations",
        "investment_theses",
        "thesis_revisions",
        "thesis_assessments",
        "company_scores",
        "portfolio_scores",
        "market_cache_entries",
        "market_fetch_attempts",
        "reports",
        "report_deliveries",
    }
)

IMPORT_TABLE_NAMES = (
    "portfolio_imports",
    "raw_portfolio_rows",
    "normalized_import_positions",
    "market_price_snapshots",
)

SCENARIO_TABLE_NAMES = (
    "saved_investment_scenarios",
    "saved_scenario_plans",
)


class SchemaMigrationError(RuntimeError):
    """Raised when a schema cannot be migrated without guessing its state."""


class SchemaMigrationRequiredError(SchemaMigrationError):
    """Raised when normal startup detects an existing database needing migration."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    table_names: tuple[str, ...]
    sqlite_statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n".join(
            (str(self.version), self.name, *self.table_names, *self.sqlite_statements)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_V1_SQLITE_STATEMENTS = (
    """CREATE TABLE portfolio_imports (
        id INTEGER NOT NULL PRIMARY KEY,
        import_key VARCHAR(64) NOT NULL,
        portfolio_id INTEGER,
        source_file_name VARCHAR(255),
        source_file_sha256 VARCHAR(64) NOT NULL,
        source_file_size INTEGER NOT NULL,
        sheet_name VARCHAR(128) NOT NULL,
        status VARCHAR(16) NOT NULL,
        marker_count INTEGER NOT NULL,
        marker_rows JSON NOT NULL,
        position_count INTEGER NOT NULL,
        unique_ticker_count INTEGER NOT NULL,
        duplicate_ticker_count INTEGER NOT NULL,
        total_cost_basis_usd VARCHAR(80) NOT NULL,
        total_import_market_value_usd VARCHAR(80) NOT NULL,
        total_import_profit_usd VARCHAR(80) NOT NULL,
        calculated_import_return_percent VARCHAR(80),
        daily_contribution_krw VARCHAR(80) NOT NULL,
        warning_codes JSON NOT NULL,
        imported_at DATETIME NOT NULL,
        CONSTRAINT uq_portfolio_import_key UNIQUE (import_key),
        CONSTRAINT ck_portfolio_import_status CHECK (status IN ('READY','BLOCKED')),
        CONSTRAINT ck_portfolio_import_file_size CHECK (source_file_size >= 0),
        CONSTRAINT ck_portfolio_import_marker_count CHECK (marker_count >= 0),
        CONSTRAINT ck_portfolio_import_position_count CHECK (position_count >= 0),
        FOREIGN KEY(portfolio_id) REFERENCES portfolios (id) ON DELETE SET NULL
    )""",
    """CREATE TABLE raw_portfolio_rows (
        id INTEGER NOT NULL PRIMARY KEY,
        import_id INTEGER NOT NULL,
        sheet_name VARCHAR(128) NOT NULL,
        row_number INTEGER NOT NULL,
        raw_ticker TEXT,
        raw_quantity TEXT,
        raw_purchase_value TEXT,
        raw_market_value TEXT,
        raw_provided_return_percent TEXT,
        raw_daily_contribution_krw TEXT,
        source_section_id VARCHAR(64),
        source_account_alias VARCHAR(64),
        source_input_type VARCHAR(32),
        parse_status VARCHAR(16) NOT NULL,
        raw_cells JSON NOT NULL,
        warning_codes JSON NOT NULL,
        imported_at DATETIME NOT NULL,
        CONSTRAINT uq_raw_import_row UNIQUE (import_id, sheet_name, row_number),
        CONSTRAINT ck_raw_import_row_number CHECK (row_number > 0),
        CONSTRAINT ck_raw_import_parse_status
            CHECK (parse_status IN ('BLANK','HEADER','MARKER','VALID','INVALID')),
        CONSTRAINT ck_raw_import_input_type
            CHECK (source_input_type IS NULL OR source_input_type IN
                ('TOTAL_POSITION_VALUE','PER_SHARE_PRICE')),
        FOREIGN KEY(import_id) REFERENCES portfolio_imports (id) ON DELETE CASCADE
    )""",
    """CREATE TABLE normalized_import_positions (
        id INTEGER NOT NULL PRIMARY KEY,
        import_id INTEGER NOT NULL,
        raw_row_id INTEGER NOT NULL,
        market VARCHAR(24) NOT NULL,
        ticker VARCHAR(32) NOT NULL,
        asset_type VARCHAR(16) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        quantity VARCHAR(80) NOT NULL,
        total_cost_basis_usd VARCHAR(80) NOT NULL,
        import_market_value_usd VARCHAR(80) NOT NULL,
        average_cost_per_share_usd VARCHAR(80),
        import_current_price_usd VARCHAR(80),
        import_profit_usd VARCHAR(80) NOT NULL,
        calculated_import_return_percent VARCHAR(80),
        provided_return_percent VARCHAR(80),
        daily_contribution_krw VARCHAR(80) NOT NULL,
        source_section_id VARCHAR(64) NOT NULL,
        source_account_alias VARCHAR(64) NOT NULL,
        source_input_type VARCHAR(32) NOT NULL,
        source_row_number INTEGER NOT NULL,
        warning_codes JSON NOT NULL,
        CONSTRAINT uq_normalized_import_raw_row UNIQUE (raw_row_id),
        CONSTRAINT ck_normalized_import_quantity CHECK (CAST(quantity AS NUMERIC) >= 0),
        CONSTRAINT ck_normalized_import_cost
            CHECK (CAST(total_cost_basis_usd AS NUMERIC) >= 0),
        CONSTRAINT ck_normalized_import_market_value
            CHECK (CAST(import_market_value_usd AS NUMERIC) >= 0),
        CONSTRAINT ck_normalized_import_contribution
            CHECK (CAST(daily_contribution_krw AS NUMERIC) >= 0),
        CONSTRAINT ck_normalized_import_input_type
            CHECK (source_input_type IN ('TOTAL_POSITION_VALUE','PER_SHARE_PRICE')),
        FOREIGN KEY(import_id) REFERENCES portfolio_imports (id) ON DELETE CASCADE,
        FOREIGN KEY(raw_row_id) REFERENCES raw_portfolio_rows (id) ON DELETE CASCADE
    )""",
    """CREATE TABLE market_price_snapshots (
        id INTEGER NOT NULL PRIMARY KEY,
        instrument_id INTEGER NOT NULL,
        price VARCHAR(80) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        as_of DATETIME NOT NULL,
        fetched_at DATETIME NOT NULL,
        provider VARCHAR(48) NOT NULL,
        source_kind VARCHAR(16) NOT NULL,
        stale BOOLEAN NOT NULL,
        CONSTRAINT uq_market_price_observation UNIQUE (instrument_id, provider, as_of),
        CONSTRAINT ck_market_price_positive CHECK (CAST(price AS NUMERIC) > 0),
        CONSTRAINT ck_market_price_source_kind
            CHECK (source_kind IN ('IMPORT','LIVE','CACHE')),
        FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_portfolio_imports_portfolio_id ON portfolio_imports (portfolio_id)",
    "CREATE INDEX ix_raw_portfolio_rows_import_id ON raw_portfolio_rows (import_id)",
    """CREATE INDEX ix_normalized_import_positions_import_id
        ON normalized_import_positions (import_id)""",
    """CREATE INDEX ix_market_price_snapshots_instrument_id
        ON market_price_snapshots (instrument_id)""",
    """CREATE INDEX ix_market_price_instrument_as_of
        ON market_price_snapshots (instrument_id, as_of)""",
)

_V2_SQLITE_STATEMENTS = (
    """CREATE TABLE saved_investment_scenarios (
        id INTEGER NOT NULL PRIMARY KEY,
        portfolio_id INTEGER NOT NULL,
        name VARCHAR(80) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT uq_saved_scenario_portfolio_name UNIQUE (portfolio_id, name),
        FOREIGN KEY(portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE
    )""",
    """CREATE TABLE saved_scenario_plans (
        id INTEGER NOT NULL PRIMARY KEY,
        scenario_id INTEGER NOT NULL,
        market VARCHAR(24) NOT NULL,
        ticker VARCHAR(32) NOT NULL,
        asset_type VARCHAR(16) NOT NULL,
        instrument_currency VARCHAR(3) NOT NULL,
        configured_amount_krw VARCHAR(80) NOT NULL,
        enabled BOOLEAN NOT NULL,
        CONSTRAINT uq_saved_scenario_instrument UNIQUE
            (scenario_id, market, ticker, asset_type, instrument_currency),
        CONSTRAINT ck_saved_scenario_amount_nonnegative
            CHECK (CAST(configured_amount_krw AS NUMERIC) >= 0),
        FOREIGN KEY(scenario_id) REFERENCES saved_investment_scenarios (id) ON DELETE CASCADE
    )""",
    """CREATE INDEX ix_saved_investment_scenarios_portfolio_id
        ON saved_investment_scenarios (portfolio_id)""",
    """CREATE INDEX ix_saved_scenario_plans_scenario_id
        ON saved_scenario_plans (scenario_id)""",
)


MIGRATIONS = (
    Migration(
        1,
        "portfolio import snapshots and market price observations",
        IMPORT_TABLE_NAMES,
        _V1_SQLITE_STATEMENTS,
    ),
    Migration(
        2,
        "durable named investment scenarios",
        SCENARIO_TABLE_NAMES,
        _V2_SQLITE_STATEMENTS,
    ),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class MigrationReport:
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]
    backup: BackupInfo | None


async def _table_names(connection: AsyncConnection) -> set[str]:
    result = await connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {str(row[0]) for row in result.fetchall()}


def _legacy_tables():
    return [Base.metadata.tables[name] for name in sorted(LEGACY_TABLE_NAMES)]


def _migration_tables(migration: Migration):
    return [Base.metadata.tables[name] for name in migration.table_names]


async def create_fresh_schema(connection: AsyncConnection) -> MigrationReport:
    """Create the latest schema in a database known to contain no user tables."""

    existing = await _table_names(connection)
    if existing:
        raise SchemaMigrationError("fresh schema creation requires an empty database")
    await connection.run_sync(Base.metadata.create_all)
    for migration in MIGRATIONS:
        await connection.execute(
            insert(SchemaMigration).values(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=datetime.now(timezone.utc),
            )
        )
    return MigrationReport(
        previous_version=0,
        current_version=LATEST_SCHEMA_VERSION,
        applied_versions=tuple(item.version for item in MIGRATIONS),
        backup=None,
    )


async def _ensure_migration_table(connection: AsyncConnection) -> None:
    await connection.run_sync(
        lambda sync_connection: SchemaMigration.__table__.create(
            sync_connection,
            checkfirst=True,
        )
    )


async def _validate_legacy_schema(connection: AsyncConnection) -> set[str]:
    existing = await _table_names(connection)
    missing = LEGACY_TABLE_NAMES - existing
    if missing:
        names = ", ".join(sorted(missing))
        raise SchemaMigrationError(f"database is missing required legacy tables: {names}")
    return existing


async def _upgrade_legacy_instrument_identity(connection: AsyncConnection) -> None:
    columns = await connection.exec_driver_sql('PRAGMA table_info("instruments")')
    names = {str(row[1]) for row in columns.fetchall()}
    if "asset_type" not in names:
        await connection.exec_driver_sql(
            "ALTER TABLE instruments "
            "ADD COLUMN asset_type VARCHAR(16) NOT NULL DEFAULT 'EQUITY'"
        )
    await connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_identity_v2 "
        "ON instruments(market, ticker, asset_type, currency)"
    )


async def current_schema_version(connection: AsyncConnection) -> int:
    existing = await _table_names(connection)
    if "schema_migrations" not in existing:
        return 0
    version = await connection.scalar(select(func.max(SchemaMigration.version)))
    return int(version or 0)


async def verify_current_schema(connection: AsyncConnection) -> int:
    """Verify a schema without mutating it."""

    existing = await _validate_legacy_schema(connection)
    if "schema_migrations" not in existing:
        raise SchemaMigrationRequiredError(
            "database schema is unversioned; run the explicit migrate-db command"
        )
    applied = list(
        (
            await connection.execute(
                select(
                    SchemaMigration.version,
                    SchemaMigration.name,
                    SchemaMigration.checksum,
                )
            )
        ).all()
    )
    known = {migration.version: migration for migration in MIGRATIONS}
    for record in applied:
        migration = known.get(record.version)
        if migration is None:
            raise SchemaMigrationError(
                f"database schema version {record.version} is newer than this application"
            )
        if record.name != migration.name or record.checksum != migration.checksum:
            raise SchemaMigrationError(
                f"database schema migration {record.version} checksum does not match"
            )
    version = max((record.version for record in applied), default=0)
    if version != LATEST_SCHEMA_VERSION:
        raise SchemaMigrationRequiredError(
            f"database schema is at version {version}; latest is {LATEST_SCHEMA_VERSION}"
        )
    required_tables = set(IMPORT_TABLE_NAMES) | set(SCENARIO_TABLE_NAMES)
    missing_latest = required_tables - existing
    if missing_latest:
        raise SchemaMigrationError(
            "database migration record exists but tables are missing: "
            + ", ".join(sorted(missing_latest))
        )
    return version


async def upgrade_schema(
    connection: AsyncConnection,
    *,
    allow_unversioned_legacy: bool,
) -> MigrationReport:
    """Apply pending migrations inside the caller's transaction."""

    existing = await _validate_legacy_schema(connection)
    if "schema_migrations" not in existing and not allow_unversioned_legacy:
        raise SchemaMigrationRequiredError("unversioned legacy schema needs explicit approval")
    await _upgrade_legacy_instrument_identity(connection)
    await _ensure_migration_table(connection)
    previous = await current_schema_version(connection)

    existing_records = {
        record.version: record
        for record in (
            await connection.execute(
                select(
                    SchemaMigration.version,
                    SchemaMigration.name,
                    SchemaMigration.checksum,
                )
            )
        ).all()
    }
    known = {migration.version: migration for migration in MIGRATIONS}
    for version, record in existing_records.items():
        migration = known.get(version)
        if migration is None:
            raise SchemaMigrationError(
                f"database schema version {version} is newer than this application"
            )
        if record.name != migration.name or record.checksum != migration.checksum:
            raise SchemaMigrationError(f"migration {version} checksum does not match")

    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in existing_records:
            continue
        for statement in migration.sqlite_statements:
            await connection.exec_driver_sql(statement)
        await connection.execute(
            insert(SchemaMigration).values(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=datetime.now(timezone.utc),
            )
        )
        applied.append(migration.version)

    return MigrationReport(
        previous_version=previous,
        current_version=LATEST_SCHEMA_VERSION,
        applied_versions=tuple(applied),
        backup=None,
    )


async def rollback_latest_for_tests(connection: AsyncConnection) -> int:
    """Rollback one migration; intentionally not exposed through the production CLI."""

    version = await current_schema_version(connection)
    migration = next((item for item in MIGRATIONS if item.version == version), None)
    if migration is None:
        raise SchemaMigrationError(f"no reversible migration found for version {version}")
    for table in reversed(_migration_tables(migration)):
        await connection.run_sync(lambda sync_connection, item=table: item.drop(sync_connection))
    await connection.execute(
        delete(SchemaMigration).where(SchemaMigration.version == migration.version)
    )
    return migration.version - 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_sqlite_backup(source: Path, backup_directory: Path) -> BackupInfo:
    source = source.resolve(strict=True)
    backup_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        backup_directory / f"{source.stem}.before-v{LATEST_SCHEMA_VERSION}.{stamp}.db"
    ).resolve()
    if destination == source:
        raise SchemaMigrationError("backup destination must differ from the source database")

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise SchemaMigrationError("SQLite backup integrity check failed")
    finally:
        destination_connection.close()
        source_connection.close()

    return BackupInfo(
        path=destination,
        sha256=_sha256(destination),
        size=destination.stat().st_size,
    )


async def create_sqlite_backup(source: Path, backup_directory: Path) -> BackupInfo:
    return await asyncio.to_thread(_create_sqlite_backup, source, backup_directory)
