from __future__ import annotations

import hashlib
import sys
import tempfile
import unicodedata
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.portfolio_import import (  # noqa: E402
    MARKER_TEXT,
    ImportPreviewStatus,
    ImportWarningCode,
    PortfolioImportError,
    PortfolioInputType,
    RawPortfolioRow,
    RawRowParseStatus,
    normalize_portfolio_row,
    parse_portfolio_rows,
    parse_portfolio_xlsx,
)

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)


def build_preview(rows):
    return parse_portfolio_rows(
        rows,
        import_id="test-import",
        sheet_name="Sheet1",
        source_file_name="portfolio.xlsx",
        source_file_sha256="a" * 64,
        source_file_size=123,
        imported_at=NOW,
    )


def write_minimal_xlsx(path: Path, rows: list[list[str | int | Decimal | None]]) -> None:
    shared: list[str] = []

    def cell_xml(row_number: int, column_number: int, value) -> str:
        letters = ""
        number = column_number
        while number:
            number, remainder = divmod(number - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        reference = f"{letters}{row_number}"
        if isinstance(value, str):
            if value not in shared:
                shared.append(value)
            return f'<c r="{reference}" t="s"><v>{shared.index(value)}</v></c>'
        return f'<c r="{reference}"><v>{escape(str(value))}</v></c>'

    row_xml = []
    for row_number, values in enumerate(rows, start=1):
        cells = "".join(
            cell_xml(row_number, column_number, value)
            for column_number, value in enumerate(values, start=1)
            if value is not None
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    shared_xml = "".join(f"<si><t>{escape(value)}</t></si>" for value in shared)
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>",
        )


class PortfolioImportTests(unittest.TestCase):
    def test_marker_switches_modes_and_is_not_a_position(self) -> None:
        preview = build_preview(
            [
                (1, ("종목명", "수량", "매입가", "평가금액", "수익률", "모으기금액")),
                (2, ("JEPQ", "2", "115.6603", "119.22", "2.93", "0")),
                (3, (f"  {MARKER_TEXT}  ",)),
                (4, ("JEPQ", "21", "56.9828", "59.61", "4.61", "3000")),
            ]
        )

        self.assertEqual(preview.status, ImportPreviewStatus.READY)
        self.assertEqual(preview.marker_rows, (3,))
        self.assertEqual(len(preview.normalized_positions), 2)
        total, per_share = preview.normalized_positions
        self.assertEqual(total.total_cost_basis_usd, Decimal("115.6603"))
        self.assertEqual(total.import_market_value_usd, Decimal("119.22"))
        self.assertEqual(total.average_cost_per_share_usd, Decimal("57.83015"))
        self.assertEqual(total.import_current_price_usd, Decimal("59.61"))
        self.assertEqual(per_share.total_cost_basis_usd, Decimal("1196.6388"))
        self.assertEqual(per_share.import_market_value_usd, Decimal("1251.81"))
        marker = next(row for row in preview.raw_rows if row.row_number == 3)
        self.assertEqual(marker.parse_status, RawRowParseStatus.MARKER)

    def test_aggregation_recalculates_weighted_values_and_contribution(self) -> None:
        preview = build_preview(
            [
                (1, ("JEPQ", "2", "120", "126", "5", "1000")),
                (2, (MARKER_TEXT,)),
                (3, ("JEPQ", "3", "40", "42", "5", "2000")),
            ]
        )
        combined = preview.aggregated_positions[0]
        self.assertEqual(combined.quantity, Decimal("5"))
        self.assertEqual(combined.total_cost_basis_usd, Decimal("240"))
        self.assertEqual(combined.import_market_value_usd, Decimal("252"))
        self.assertEqual(combined.average_cost_per_share_usd, Decimal("48"))
        self.assertEqual(combined.import_current_price_usd, Decimal("50.4"))
        self.assertEqual(combined.calculated_import_return_percent, Decimal("5.00"))
        self.assertEqual(combined.daily_contribution_krw, Decimal("3000"))

    def test_missing_and_multiple_markers_block_preview(self) -> None:
        missing = build_preview([(1, ("AAA", "1", "10", "11", "10", "0"))])
        multiple = build_preview(
            [
                (1, (MARKER_TEXT,)),
                (2, (MARKER_TEXT,)),
                (3, ("AAA", "1", "10", "11", "10", "0")),
            ]
        )
        self.assertEqual(missing.status, ImportPreviewStatus.BLOCKED)
        self.assertIn(ImportWarningCode.MISSING_MARKER, {w.code for w in missing.warnings})
        self.assertEqual(multiple.status, ImportPreviewStatus.BLOCKED)
        self.assertIn(
            ImportWarningCode.MULTIPLE_MARKERS,
            {warning.code for warning in multiple.warnings},
        )

    def test_blank_header_and_unicode_normalized_marker(self) -> None:
        full_width_marker = "여기 아래는 주당가격"
        preview = build_preview(
            [
                (1, ("종목명", "수량")),
                (2, (None, None)),
                (3, (unicodedata.normalize("NFD", full_width_marker),)),
                (4, ("AAA", "1", "10", "11", "10", "0")),
            ]
        )
        statuses = [row.parse_status for row in preview.raw_rows]
        self.assertEqual(
            statuses,
            [
                RawRowParseStatus.HEADER,
                RawRowParseStatus.BLANK,
                RawRowParseStatus.MARKER,
                RawRowParseStatus.VALID,
            ],
        )

    def test_validation_warnings_and_invalid_rows(self) -> None:
        preview = build_preview(
            [
                (1, ("BAD TICKER", "1", "10", "11", "10", "0")),
                (2, ("NEGQ", "-1", "10", "11", "10", "0")),
                (3, ("NANX", "NaN", "10", "11", "10", "0")),
                (4, ("NEGM", "1", "10", "-1", "-110", "0")),
                (5, ("NEGC", "1", "10", "11", "10", "-1")),
                (6, (MARKER_TEXT,)),
            ]
        )
        codes = {warning.code for warning in preview.warnings}
        self.assertTrue(
            {
                ImportWarningCode.INVALID_TICKER,
                ImportWarningCode.NEGATIVE_QUANTITY,
                ImportWarningCode.INVALID_NUMERIC,
                ImportWarningCode.NEGATIVE_MARKET_VALUE,
                ImportWarningCode.NEGATIVE_CONTRIBUTION,
            }.issubset(codes)
        )
        self.assertEqual(preview.status, ImportPreviewStatus.BLOCKED)

    def test_return_mismatch_uses_calculated_return(self) -> None:
        preview = build_preview(
            [
                (1, ("TLT", "2", "100", "220", "-1.45", "0")),
                (2, (MARKER_TEXT,)),
            ]
        )
        position = preview.normalized_positions[0]
        self.assertEqual(position.calculated_import_return_percent, Decimal("120"))
        self.assertIn(
            ImportWarningCode.SOURCE_RETURN_MISMATCH,
            {warning.code for warning in position.warnings},
        )

    def test_zero_quantity_is_valid_only_for_a_funded_new_etf(self) -> None:
        valid = build_preview(
            [
                (1, (MARKER_TEXT,)),
                (2, ("VT", "0", "100", "110", "0", "3000")),
            ]
        )
        invalid = build_preview(
            [
                (1, (MARKER_TEXT,)),
                (2, ("VT", "0", "100", "110", "0", "0")),
            ]
        )
        self.assertEqual(valid.status, ImportPreviewStatus.READY)
        self.assertEqual(valid.normalized_positions[0].total_cost_basis_usd, Decimal("0"))
        self.assertEqual(invalid.status, ImportPreviewStatus.BLOCKED)

    def test_explicit_source_section_mismatch_is_blocking(self) -> None:
        row = RawPortfolioRow(
            import_id="test",
            sheet_name="Sheet1",
            row_number=2,
            raw_ticker="AAA",
            raw_quantity="1",
            raw_purchase_value="10",
            raw_market_value="11",
            raw_provided_return_percent="10",
            raw_daily_contribution_krw="0",
            source_section_id="excel-total-section",
            source_account_alias="excel-total-section",
            detected_input_type=PortfolioInputType.TOTAL_POSITION_VALUE,
            parse_status=RawRowParseStatus.VALID,
            raw_cells=("AAA", "1", "10", "11", "10", "0"),
            imported_at=NOW,
        )
        parsed, normalized = normalize_portfolio_row(
            row,
            expected_input_type=PortfolioInputType.PER_SHARE_PRICE,
        )
        self.assertIsNone(normalized)
        self.assertEqual(parsed.parse_status, RawRowParseStatus.INVALID)
        self.assertIn(
            ImportWarningCode.SOURCE_SECTION_MISMATCH,
            {warning.code for warning in parsed.warnings},
        )

    def test_api_serialization_keeps_decimals_as_strings(self) -> None:
        preview = build_preview(
            [
                (1, ("AAA", "1", "10.123456789", "11", "8.660990", "1000")),
                (2, (MARKER_TEXT,)),
            ]
        )
        payload = preview.to_api_dict()
        self.assertEqual(payload["summary"]["total_cost_basis_usd"], "10.123456789")
        self.assertIsInstance(payload["normalized_positions"][0]["quantity"], str)
        self.assertEqual(
            payload["normalized_positions"][0]["instrument"]["assetType"],
            "ETF",
        )

    def test_binary_float_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(PortfolioImportError, "binary float"):
            build_preview(
                [
                    (1, ("AAA", 1.25, "10", "11", "10", "0")),
                    (2, (MARKER_TEXT,)),
                ]
            )

    def test_xlsx_reader_integration_uses_shared_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.xlsx"
            write_minimal_xlsx(
                path,
                [
                    ["종목명", "수량", "매입가", "평가금액", "수익률", "모으기금액"],
                    ["AAA", Decimal("2"), Decimal("100"), Decimal("110"), 10, 0],
                    [MARKER_TEXT],
                    ["BBB", Decimal("3"), Decimal("20"), Decimal("22"), 10, 1000],
                ],
            )
            preview = parse_portfolio_xlsx(path, imported_at=NOW, import_id="xlsx-test")
            expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(preview.status, ImportPreviewStatus.READY)
        self.assertEqual(preview.marker_rows, (3,))
        self.assertEqual(preview.summary.total_cost_basis_usd, Decimal("160"))
        self.assertEqual(preview.source_file_sha256, expected_sha256)


if __name__ == "__main__":
    unittest.main()
