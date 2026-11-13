"""Async database lifecycle and SQLite safety configuration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .migrations import (
    BackupInfo,
    MigrationReport,
    create_fresh_schema,
    create_sqlite_backup,
    upgrade_schema,
    verify_current_schema,
)
from .models import Base


class DatabaseConfigurationError(ValueError):
    """Raised for a synchronous or malformed database URL."""


class Database:
    """Own the async engine and short-lived session factory.

    SQLite connections enable foreign keys, WAL, a bounded busy timeout, and
    NORMAL synchronous mode. Each bot handler/job should request its own
    session; an ``AsyncSession`` must never be shared by concurrent tasks.
    """

    def __init__(
        self,
        url: str,
        *,
        sqlite_busy_timeout_ms: int = 5_000,
        echo: bool = False,
    ) -> None:
        if sqlite_busy_timeout_ms < 1 or sqlite_busy_timeout_ms > 120_000:
            raise DatabaseConfigurationError("sqlite_busy_timeout_ms must be between 1 and 120000")
        try:
            parsed = make_url(url)
        except ArgumentError:
            raise DatabaseConfigurationError("database URL is malformed") from None
        if not parsed.drivername.startswith("sqlite+") and "+" not in parsed.drivername:
            raise DatabaseConfigurationError("an async SQLAlchemy driver is required")
        if parsed.drivername == "sqlite" or parsed.drivername == "postgresql":
            raise DatabaseConfigurationError("a synchronous database driver is not allowed")
        if parsed.drivername.startswith("sqlite+") and parsed.drivername != "sqlite+aiosqlite":
            raise DatabaseConfigurationError("SQLite must use the aiosqlite driver")

        self._url: URL = parsed
        self._sqlite_busy_timeout_ms = sqlite_busy_timeout_ms
        self.engine: AsyncEngine = create_async_engine(
            parsed,
            echo=echo,
            pool_pre_ping=not parsed.drivername.startswith("sqlite+"),
        )
        if parsed.drivername == "sqlite+aiosqlite":
            self._configure_sqlite(self.engine, sqlite_busy_timeout_ms)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _configure_sqlite(engine: AsyncEngine, busy_timeout_ms: int) -> None:
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    @property
    def backend(self) -> str:
        return self._url.drivername

    def _sqlite_database_path(self) -> Path | None:
        if self._url.drivername != "sqlite+aiosqlite":
            return None
        database = self._url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            return None
        return Path(database)

    async def initialize(self) -> None:
        database_path = self._sqlite_database_path()
        from account_bot.polling_guard import require_rollback_clear
        require_rollback_clear(database_path)
        is_fresh = (
            database_path is None
            or not database_path.exists()
            or database_path.stat().st_size == 0
        )
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as connection:
            if self._url.drivername != "sqlite+aiosqlite":
                raise DatabaseConfigurationError("schema migrations currently support SQLite only")
            if is_fresh:
                await create_fresh_schema(connection)
            else:
                await verify_current_schema(connection)

    async def migrate_to_latest(self, *, backup_directory: Path) -> MigrationReport:
        """Backup and explicitly migrate an existing SQLite database."""

        database_path = self._sqlite_database_path()
        if database_path is None:
            raise DatabaseConfigurationError(
                "backup-first migration requires a file-backed SQLite database"
            )
        database_path.parent.mkdir(parents=True, exist_ok=True)
        is_fresh = not database_path.exists() or database_path.stat().st_size == 0
        backup: BackupInfo | None = None
        if not is_fresh:
            backup = await create_sqlite_backup(database_path, backup_directory)
        async with self.engine.begin() as connection:
            if is_fresh:
                report = await create_fresh_schema(connection)
            else:
                report = await upgrade_schema(connection, allow_unversioned_legacy=True)
        return MigrationReport(
            previous_version=report.previous_version,
            current_version=report.current_version,
            applied_versions=report.applied_versions,
            backup=backup,
        )

    async def drop_schema_for_tests(self) -> None:
        """Drop all tables. This method is intentionally explicit and test-only."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.session() as session:
            async with session.begin():
                yield session

    async def dispose(self) -> None:
        await self.engine.dispose()

    def __repr__(self) -> str:
        safe_url = self._url.render_as_string(hide_password=True)
        return f"Database(url={safe_url!r})"
