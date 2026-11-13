from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.portfolio_import import (  # noqa: E402
    MARKER_TEXT,
    PortfolioImportStore,
    parse_portfolio_rows,
)
from account_bot.storage import (  # noqa: E402
    Database,
    NormalizedPortfolioImportPosition,
    PortfolioImportBatch,
)


class PortfolioImportStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "import.db"
        self.database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        await self.database.initialize()

    async def asyncTearDown(self) -> None:
        await self.database.dispose()
        self.temporary_directory.cleanup()

    async def test_preview_persists_raw_and_normalized_snapshots_idempotently(self) -> None:
        preview = parse_portfolio_rows(
            [
                (1, ("AAA", "1.25", "10.123456789", "11.5", "13.596", "1000")),
                (2, (MARKER_TEXT,)),
                (3, ("BBB", "2", "20", "21", "5", "2000")),
            ],
            import_id="stable-import-key",
            sheet_name="Sheet1",
            source_file_name="C:/private/path/portfolio.xlsx",
            source_file_sha256="b" * 64,
            source_file_size=456,
            imported_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        store = PortfolioImportStore(self.database)

        first = await store.save_preview(preview)
        repeated = await store.save_preview(preview)

        self.assertEqual(first, repeated)
        self.assertEqual(first.raw_rows, 3)
        self.assertEqual(first.normalized_positions, 2)
        async with self.database.session() as session:
            batches = list((await session.scalars(select(PortfolioImportBatch))).all())
            normalized = list(
                (
                    await session.scalars(
                        select(NormalizedPortfolioImportPosition).order_by(
                            NormalizedPortfolioImportPosition.source_row_number
                        )
                    )
                ).all()
            )

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].source_file_name, "portfolio.xlsx")
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0].quantity, Decimal("1.250000000000000000"))
        self.assertEqual(
            normalized[0].total_cost_basis_usd,
            Decimal("10.123456789000000000"),
        )
        self.assertEqual(normalized[1].total_cost_basis_usd, Decimal("40.000000000000000000"))


if __name__ == "__main__":
    unittest.main()
