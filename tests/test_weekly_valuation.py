from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.market.yfinance_provider import _weekly_close  # noqa: E402


class WeeklyCloseValuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            (date(2026, 8, 20), Decimal("100")),
            (date(2026, 8, 21), Decimal("101")),
            (date(2026, 8, 24), Decimal("102")),
            (date(2026, 8, 25), Decimal("103")),
        ]

    def test_midweek_analysis_keeps_previous_friday_close(self) -> None:
        close, as_of = _weekly_close(
            self.rows,
            now=datetime(2026, 8, 26, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(close, Decimal("101"))
        self.assertEqual(as_of.date(), date(2026, 8, 21))

    def test_saturday_analysis_uses_just_completed_friday(self) -> None:
        rows = self.rows + [(date(2026, 8, 28), Decimal("105"))]
        close, as_of = _weekly_close(
            rows,
            now=datetime(2026, 8, 29, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(close, Decimal("105"))
        self.assertEqual(as_of.date(), date(2026, 8, 28))

    def test_friday_holiday_uses_last_session_of_that_week(self) -> None:
        rows = [(date(2026, 8, 27), Decimal("104"))]
        close, as_of = _weekly_close(
            rows,
            now=datetime(2026, 8, 29, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(close, Decimal("104"))
        self.assertEqual(as_of.date(), date(2026, 8, 27))


if __name__ == "__main__":
    unittest.main()
