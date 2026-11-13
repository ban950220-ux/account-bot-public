from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.portfolio_import import (  # noqa: E402
    ImportPreviewStatus,
    ImportWarningCode,
    parse_portfolio_xlsx,
)


class PrivatePortfolioWorkbookTests(unittest.TestCase):
    """Optional regression against a local workbook without committing its data."""

    def test_private_workbook_matches_caller_supplied_expectations(self) -> None:
        workbook = os.environ.get("ACCOUNT_BOT_PRIVATE_IMPORT_XLSX")
        expected_json = os.environ.get("ACCOUNT_BOT_PRIVATE_IMPORT_EXPECTED")
        if not workbook or not expected_json:
            self.skipTest("private workbook regression environment is not configured")

        expected = json.loads(expected_json, parse_float=Decimal)
        preview = parse_portfolio_xlsx(workbook, sheet_name=expected["sheet_name"])
        summary = preview.summary

        self.assertEqual(preview.status, ImportPreviewStatus.READY)
        self.assertEqual(list(preview.marker_rows), expected["marker_rows"])
        self.assertEqual(summary.position_count, expected["position_count"])
        self.assertEqual(summary.unique_ticker_count, expected["unique_ticker_count"])
        self.assertEqual(summary.duplicate_ticker_count, expected["duplicate_ticker_count"])
        self.assertEqual(summary.above_marker_count, expected["above_marker_count"])
        self.assertEqual(summary.below_marker_count, expected["below_marker_count"])
        self.assertEqual(
            summary.total_cost_basis_usd,
            Decimal(expected["total_cost_basis_usd"]),
        )
        self.assertEqual(
            summary.total_import_market_value_usd,
            Decimal(expected["total_import_market_value_usd"]),
        )
        self.assertEqual(
            summary.total_import_profit_usd,
            Decimal(expected["total_import_profit_usd"]),
        )
        self.assertEqual(
            summary.calculated_import_return_percent,
            Decimal(expected["calculated_import_return_percent"]),
        )
        self.assertEqual(
            summary.daily_contribution_krw,
            Decimal(expected["daily_contribution_krw"]),
        )
        self.assertEqual(
            summary.annual_contribution_krw,
            Decimal(expected["annual_contribution_krw"]),
        )

        aggregate = next(
            item
            for item in preview.aggregated_positions
            if item.instrument.ticker == expected["golden_ticker"]
        )
        self.assertEqual(
            len(aggregate.source_positions), expected["golden_source_count"]
        )
        for field_name in (
            "quantity",
            "total_cost_basis_usd",
            "import_market_value_usd",
            "average_cost_per_share_usd",
            "import_current_price_usd",
            "calculated_import_return_percent",
            "daily_contribution_krw",
        ):
            self.assertEqual(
                getattr(aggregate, field_name),
                Decimal(expected["golden_aggregate"][field_name]),
            )

        mismatch_tickers = {
            warning.ticker
            for warning in preview.warnings
            if warning.code == ImportWarningCode.SOURCE_RETURN_MISMATCH
        }
        self.assertTrue(set(expected["required_mismatch_tickers"]).issubset(mismatch_tickers))


if __name__ == "__main__":
    unittest.main()
