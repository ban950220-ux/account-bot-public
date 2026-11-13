"""Import one private workbook into an explicit local web-dashboard SQLite DB."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from account_bot.portfolio_import import (
    ImportPreviewStatus,
    PortfolioImportStore,
    parse_portfolio_xlsx,
)
from account_bot.storage import Database, SQLitePortfolioRepository


async def import_workbook(workbook: Path, database_path: Path, sheet_name: str) -> None:
    preview = parse_portfolio_xlsx(workbook, sheet_name=sheet_name)
    if preview.status != ImportPreviewStatus.READY:
        raise SystemExit("import preview is blocked; review workbook warnings before saving")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}")
    try:
        await database.initialize()
        await SQLitePortfolioRepository(database).initialize_default_portfolio()
        counts = await PortfolioImportStore(database).save_preview(preview)
    finally:
        await database.dispose()
    print(
        "import READY",
        "raw_rows",
        counts.raw_rows,
        "positions",
        preview.summary.position_count,
        "unique_tickers",
        preview.summary.unique_ticker_count,
        "daily_contribution_krw",
        preview.summary.daily_contribution_krw,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("--sheet", default="Sheet1")
    args = parser.parse_args()
    asyncio.run(import_workbook(args.workbook, args.database, args.sheet))


if __name__ == "__main__":
    main()
