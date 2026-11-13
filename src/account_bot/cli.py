"""Command-line entry points for setup, diagnostics, and polling."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

from account_bot.config import Settings, SettingsError
from account_bot.local_control import local_status, request_stop
from account_bot.logging_config import configure_logging
from account_bot.storage import SchemaMigrationError


def _configured(value: bool) -> str:
    return "configured" if value else "not configured"


def doctor(settings: Settings) -> int:
    status = settings.credential_status()
    print(f"TELEGRAM credentials: {_configured(status['telegram'])}")
    print(f"KIWOOM MAIN credentials: {_configured(status['kiwoom_main'])}")
    print(f"KIWOOM ACCUM credentials: {_configured(status['kiwoom_accum'])}")
    print(
        "TOSS source: "
        + ("configured" if status["toss"] or settings.toss_snapshot_path else "excluded")
    )
    print("MERITZ API: not used (normalized snapshot mode)")
    print("TELEGRAM allowlist: " + _configured(bool(settings.allowed_telegram_user_ids)))
    print("FINGERPRINT pepper: " + _configured(bool(settings.fingerprint_pepper)))
    print("MERITZ normalized snapshot: " + _configured(settings.meritz_snapshot_path is not None))
    print(
        "MERITZ recurring snapshot: "
        + _configured(settings.meritz_accumul_snapshot_path is not None)
    )
    print("MARKET data provider: " + _configured(importlib.util.find_spec("yfinance") is not None))
    print(f"DATABASE backend: {settings.database_url.split(':', 1)[0]}")
    print(f"SCHEDULER: {'enabled' if settings.scheduler_enabled else 'disabled'}")
    print("BROKER policy: READ ONLY")
    return 0


async def _initialize_database(settings: Settings) -> None:
    from account_bot.app import build_runtime

    runtime = build_runtime(settings)
    try:
        await runtime.database.initialize()
        await runtime.repository.initialize_default_portfolio()
    finally:
        for broker in runtime.brokers:
            await broker.aclose()
        await runtime.database.dispose()


async def _migrate_database(settings: Settings, backup_directory: Path) -> None:
    from account_bot.storage import Database

    database = Database(
        settings.database_url,
        sqlite_busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    try:
        report = await database.migrate_to_latest(backup_directory=backup_directory)
    finally:
        await database.dispose()
    print(
        f"Database schema migrated from v{report.previous_version} "
        f"to v{report.current_version}."
    )
    if report.backup is not None:
        print(f"Verified backup: {report.backup.path}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Read-only multi-broker account bot")
    result.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="dotenv file (default: ./.env)",
    )
    result.add_argument("--verbose", action="store_true", help="verbose application logs")
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="show configuration status without values")
    subparsers.add_parser("init-db", help="initialize the local database")
    migrate = subparsers.add_parser(
        "migrate-db",
        help="backup and migrate an existing local SQLite database",
    )
    migrate.add_argument(
        "--backup-dir",
        type=Path,
        required=True,
        help="directory for a verified pre-migration SQLite backup",
    )
    subparsers.add_parser("run", help="start Telegram long polling")
    subparsers.add_parser("status", help="show local runtime lock and launch version")
    subparsers.add_parser("stop", help="request graceful shutdown of this local runtime")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings.load(env_file=args.env_file)
        if args.command == "status":
            snapshot = local_status(settings.data_dir)
            status = snapshot["status"]
            if not snapshot["running"]:
                print("Account bot: not running (local lock is free)")
                if status is not None:
                    print(f"Last recorded state: {status.get('state', 'unknown')}")
            elif status is None:
                print("Account bot: running; control status unavailable")
                return 2
            else:
                state = status["state"]
                print(f"Account bot: {'finalizing' if state == 'stopped' else state}")
                print(f"Process ID: {status.get('pid', 'unknown')}")
                print(f"Started at: {status.get('started_at', 'unknown')}")
                print(f"Package version: {status.get('package_version', 'unknown')}")
                print(
                    "Weekly report SHA-256 at start: "
                    f"{status.get('weekly_report_sha256_at_start', 'unknown')}"
                )
                print(
                    "Runtime source SHA-256 at start: "
                    f"{status.get('runtime_source_sha256_at_start', 'unknown')}"
                )
            return 0
        if args.command == "stop":
            run_id = request_stop(settings.data_dir)
            print(f"Graceful shutdown requested for run {run_id}; check status for completion.")
            return 0
        settings.ensure_runtime_directories()
        configure_logging(settings.log_dir, verbose=args.verbose)
        if args.command == "doctor":
            return doctor(settings)
        if args.command == "init-db":
            asyncio.run(_initialize_database(settings))
            print("Database initialized.")
            return 0
        if args.command == "migrate-db":
            asyncio.run(_migrate_database(settings, args.backup_dir))
            return 0
        if args.command == "run":
            from account_bot.app import run_polling
            from account_bot.runtime_lock import AlreadyRunningError, single_instance

            try:
                with single_instance(settings.data_dir / "account_bot.lock"):
                    run_polling(settings)
            except AlreadyRunningError:
                print("Account bot is already running; duplicate start ignored.")
                return 2
            return 0
    except (SettingsError, SchemaMigrationError) as exc:
        print(f"Startup error: {exc}")
        return 2
    except (RuntimeError, OSError) as exc:
        if args.command not in {"status", "stop"}:
            raise
        print(f"Local control error: {exc}")
        return 2
    return 2
