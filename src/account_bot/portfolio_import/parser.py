"""Read and normalize the two-section local ETF workbook format."""

from __future__ import annotations

import hashlib
import posixpath
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .models import (
    AggregatedPosition,
    ImportPreview,
    ImportPreviewStatus,
    ImportSummary,
    ImportWarning,
    ImportWarningCode,
    ImportWarningSeverity,
    NormalizedPosition,
    PortfolioInputType,
    RawPortfolioRow,
    RawRowParseStatus,
    us_etf,
)

MARKER_TEXT = "여기 아래는 주당가격"
TOTAL_SECTION_ID = "excel-total-section"
PER_SHARE_SECTION_ID = "excel-per-share-section"
SOURCE_RETURN_MISMATCH_THRESHOLD = Decimal("0.50")
DEFAULT_TRADING_DAYS_PER_YEAR = Decimal("252")
MAX_XLSX_BYTES = 20 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_WORKSHEET_ROWS = 10_000
MAX_WORKSHEET_COLUMNS = 64

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,31}$")
_HEADER_FIRST_CELLS = frozenset({"종목", "종목명", "티커", "ticker", "symbol"})
_EXCEL_DECIMAL_CONTEXT = Context(prec=15, rounding=ROUND_HALF_EVEN)


class PortfolioImportError(ValueError):
    """Safe workbook or import-profile validation failure."""


def normalize_text(value: str | None) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _raw(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        raise PortfolioImportError("binary float input is not accepted")
    return str(value)


def _blocking(
    code: ImportWarningCode,
    message: str,
    *,
    row_number: int | None = None,
    ticker: str | None = None,
) -> ImportWarning:
    return ImportWarning(
        code=code,
        severity=ImportWarningSeverity.BLOCKING,
        message=message,
        row_number=row_number,
        ticker=ticker,
    )


def _warning(
    code: ImportWarningCode,
    message: str,
    *,
    row_number: int | None = None,
    ticker: str | None = None,
) -> ImportWarning:
    return ImportWarning(
        code=code,
        severity=ImportWarningSeverity.WARNING,
        message=message,
        row_number=row_number,
        ticker=ticker,
    )


def _parse_decimal(raw_value: str | None, *, name: str, required: bool) -> Decimal | None:
    text = normalize_text(raw_value)
    if not text:
        if required:
            raise PortfolioImportError(f"{name} is required")
        return None
    normalized = text.replace(",", "").replace("$", "").replace("₩", "")
    if normalized.endswith("%"):
        normalized = normalized[:-1].strip()
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        raise PortfolioImportError(f"{name} is not a valid decimal") from None
    if not value.is_finite():
        raise PortfolioImportError(f"{name} must be finite")
    return value.normalize(_EXCEL_DECIMAL_CONTEXT)


def _row_values(values: Sequence[object | None]) -> tuple[str | None, ...]:
    return tuple(_raw(value) for value in values)


def _raw_row(
    *,
    import_id: str,
    sheet_name: str,
    row_number: int,
    values: Sequence[object | None],
    mode: PortfolioInputType | None,
    status: RawRowParseStatus,
    imported_at: datetime,
    source_aliases: dict[PortfolioInputType, str],
    warnings: tuple[ImportWarning, ...] = (),
) -> RawPortfolioRow:
    padded = list(_row_values(values)) + [None] * 6
    section_id = None
    alias = None
    if mode is not None:
        section_id = (
            TOTAL_SECTION_ID
            if mode == PortfolioInputType.TOTAL_POSITION_VALUE
            else PER_SHARE_SECTION_ID
        )
        alias = source_aliases[mode]
    return RawPortfolioRow(
        import_id=import_id,
        sheet_name=sheet_name,
        row_number=row_number,
        raw_ticker=padded[0],
        raw_quantity=padded[1],
        raw_purchase_value=padded[2],
        raw_market_value=padded[3],
        raw_provided_return_percent=padded[4],
        raw_daily_contribution_krw=padded[5],
        source_section_id=section_id,
        source_account_alias=alias,
        detected_input_type=mode,
        parse_status=status,
        raw_cells=tuple(padded[:MAX_WORKSHEET_COLUMNS]),
        imported_at=imported_at,
        warnings=warnings,
    )


def normalize_portfolio_row(
    row: RawPortfolioRow,
    *,
    expected_input_type: PortfolioInputType | None = None,
) -> tuple[RawPortfolioRow, NormalizedPosition | None]:
    """Normalize one position row without ever accepting binary floats."""

    warnings: list[ImportWarning] = []
    ticker = normalize_text(row.raw_ticker).upper()
    if not _TICKER_PATTERN.fullmatch(ticker):
        warnings.append(
            _blocking(
                ImportWarningCode.INVALID_TICKER,
                "ticker is malformed",
                row_number=row.row_number,
                ticker=ticker or None,
            )
        )
    if row.detected_input_type is None:
        warnings.append(
            _blocking(
                ImportWarningCode.SOURCE_SECTION_MISMATCH,
                "position row has no detected input section",
                row_number=row.row_number,
                ticker=ticker or None,
            )
        )
    elif expected_input_type is not None and row.detected_input_type != expected_input_type:
        warnings.append(
            _blocking(
                ImportWarningCode.SOURCE_SECTION_MISMATCH,
                "position row does not match the expected input section",
                row_number=row.row_number,
                ticker=ticker or None,
            )
        )

    try:
        quantity = _parse_decimal(row.raw_quantity, name="quantity", required=True)
        purchase = _parse_decimal(row.raw_purchase_value, name="purchase value", required=True)
        market = _parse_decimal(row.raw_market_value, name="market value", required=True)
        provided_return = _parse_decimal(
            row.raw_provided_return_percent,
            name="provided return percent",
            required=False,
        )
        contribution = _parse_decimal(
            row.raw_daily_contribution_krw,
            name="daily contribution KRW",
            required=False,
        )
    except PortfolioImportError as exc:
        warnings.append(
            _blocking(
                ImportWarningCode.INVALID_NUMERIC,
                str(exc),
                row_number=row.row_number,
                ticker=ticker or None,
            )
        )
        invalid = replace(
            row,
            parse_status=RawRowParseStatus.INVALID,
            warnings=tuple(warnings),
        )
        return invalid, None

    assert quantity is not None and purchase is not None and market is not None
    contribution = contribution or Decimal("0")
    if quantity < 0:
        warnings.append(
            _blocking(
                ImportWarningCode.NEGATIVE_QUANTITY,
                "quantity must not be negative",
                row_number=row.row_number,
                ticker=ticker,
            )
        )
    if market < 0:
        warnings.append(
            _blocking(
                ImportWarningCode.NEGATIVE_MARKET_VALUE,
                "market value or current price must not be negative",
                row_number=row.row_number,
                ticker=ticker,
            )
        )
    if contribution < 0:
        warnings.append(
            _blocking(
                ImportWarningCode.NEGATIVE_CONTRIBUTION,
                "daily contribution must not be negative",
                row_number=row.row_number,
                ticker=ticker,
            )
        )

    mode = row.detected_input_type
    if mode == PortfolioInputType.TOTAL_POSITION_VALUE:
        total_cost = purchase
        total_market = market
        average_cost = total_cost / quantity if quantity > 0 else None
        import_price = total_market / quantity if quantity > 0 else None
    elif mode == PortfolioInputType.PER_SHARE_PRICE:
        total_cost = quantity * purchase
        total_market = quantity * market
        average_cost = purchase
        import_price = market
    else:
        total_cost = Decimal("0")
        total_market = Decimal("0")
        average_cost = None
        import_price = None

    valid_zero_position = (
        quantity == 0
        and total_cost == 0
        and total_market == 0
        and contribution > 0
    )
    if quantity == 0 and (total_cost != 0 or total_market != 0):
        warnings.append(
            _blocking(
                ImportWarningCode.ZERO_QUANTITY_WITH_VALUE,
                "zero quantity cannot carry a non-zero position value",
                row_number=row.row_number,
                ticker=ticker,
            )
        )
    if quantity == 0 and not valid_zero_position:
        warnings.append(
            _blocking(
                ImportWarningCode.ZERO_QUANTITY_WITHOUT_PLAN,
                "zero quantity requires a positive daily contribution",
                row_number=row.row_number,
                ticker=ticker,
            )
        )
    if total_cost <= 0 and not valid_zero_position:
        warnings.append(
            _blocking(
                ImportWarningCode.NONPOSITIVE_COST_BASIS,
                "cost basis must be positive for a held position",
                row_number=row.row_number,
                ticker=ticker,
            )
        )

    calculated_return = None
    if total_cost > 0:
        calculated_return = (total_market / total_cost - Decimal("1")) * Decimal("100")
        if (
            provided_return is not None
            and abs(calculated_return - provided_return)
            > SOURCE_RETURN_MISMATCH_THRESHOLD
        ):
            warnings.append(
                _warning(
                    ImportWarningCode.SOURCE_RETURN_MISMATCH,
                    "provided return differs from the amount-based return by more than 0.50pp",
                    row_number=row.row_number,
                    ticker=ticker,
                )
            )

    blocking = any(item.severity == ImportWarningSeverity.BLOCKING for item in warnings)
    parsed_row = replace(
        row,
        parse_status=RawRowParseStatus.INVALID if blocking else RawRowParseStatus.VALID,
        warnings=tuple(warnings),
    )
    if blocking:
        return parsed_row, None

    assert mode is not None
    normalized = NormalizedPosition(
        instrument=us_etf(ticker),
        quantity=quantity,
        total_cost_basis_usd=total_cost,
        import_market_value_usd=total_market,
        import_current_price_usd=import_price,
        average_cost_per_share_usd=average_cost,
        import_profit_usd=total_market - total_cost,
        calculated_import_return_percent=calculated_return,
        provided_return_percent=provided_return,
        daily_contribution_krw=contribution,
        source_account_alias=row.source_account_alias or "unmapped-import-source",
        source_input_type=mode,
        source_import_id=row.import_id,
        source_row_number=row.row_number,
        source_section_id=row.source_section_id or "unmapped-import-section",
        warnings=tuple(warnings),
    )
    return parsed_row, normalized


def aggregate_positions(
    positions: Iterable[NormalizedPosition],
) -> tuple[AggregatedPosition, ...]:
    groups: dict[object, list[NormalizedPosition]] = defaultdict(list)
    for position in positions:
        groups[position.instrument].append(position)

    aggregated: list[AggregatedPosition] = []
    for instrument, source_positions in sorted(
        groups.items(), key=lambda item: item[0].stable_id
    ):
        quantity = sum((item.quantity for item in source_positions), Decimal("0"))
        cost = sum((item.total_cost_basis_usd for item in source_positions), Decimal("0"))
        market = sum(
            (item.import_market_value_usd for item in source_positions), Decimal("0")
        )
        contribution = sum(
            (item.daily_contribution_krw for item in source_positions), Decimal("0")
        )
        warnings = tuple(
            warning for item in source_positions for warning in item.warnings
        )
        aggregated.append(
            AggregatedPosition(
                instrument=instrument,
                quantity=quantity,
                total_cost_basis_usd=cost,
                import_market_value_usd=market,
                average_cost_per_share_usd=cost / quantity if quantity > 0 else None,
                import_current_price_usd=market / quantity if quantity > 0 else None,
                import_profit_usd=market - cost,
                calculated_import_return_percent=(
                    (market / cost - Decimal("1")) * Decimal("100") if cost > 0 else None
                ),
                daily_contribution_krw=contribution,
                source_positions=tuple(source_positions),
                warnings=warnings,
            )
        )
    return tuple(aggregated)


def parse_portfolio_rows(
    rows: Iterable[tuple[int, Sequence[object | None]]],
    *,
    import_id: str,
    sheet_name: str,
    source_file_name: str,
    source_file_sha256: str,
    source_file_size: int,
    imported_at: datetime,
    source_aliases: dict[PortfolioInputType, str] | None = None,
) -> ImportPreview:
    if imported_at.tzinfo is None or imported_at.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")
    aliases = source_aliases or {
        PortfolioInputType.TOTAL_POSITION_VALUE: TOTAL_SECTION_ID,
        PortfolioInputType.PER_SHARE_PRICE: PER_SHARE_SECTION_ID,
    }
    if set(aliases) != set(PortfolioInputType):
        raise ValueError("source aliases must cover both workbook input types")

    mode = PortfolioInputType.TOTAL_POSITION_VALUE
    marker_rows: list[int] = []
    raw_rows: list[RawPortfolioRow] = []
    positions: list[NormalizedPosition] = []

    for row_number, values in rows:
        if row_number <= 0:
            raise PortfolioImportError("worksheet row numbers must be positive")
        if row_number > MAX_WORKSHEET_ROWS:
            raise PortfolioImportError("worksheet contains too many rows")
        values_tuple = tuple(values[:MAX_WORKSHEET_COLUMNS])
        if all(not normalize_text(_raw(value)) for value in values_tuple):
            raw_rows.append(
                _raw_row(
                    import_id=import_id,
                    sheet_name=sheet_name,
                    row_number=row_number,
                    values=values_tuple,
                    mode=mode,
                    status=RawRowParseStatus.BLANK,
                    imported_at=imported_at,
                    source_aliases=aliases,
                )
            )
            continue

        first_cell = normalize_text(_raw(values_tuple[0] if values_tuple else None))
        if first_cell == MARKER_TEXT:
            marker_rows.append(row_number)
            raw_rows.append(
                _raw_row(
                    import_id=import_id,
                    sheet_name=sheet_name,
                    row_number=row_number,
                    values=values_tuple,
                    mode=None,
                    status=RawRowParseStatus.MARKER,
                    imported_at=imported_at,
                    source_aliases=aliases,
                )
            )
            mode = PortfolioInputType.PER_SHARE_PRICE
            continue

        if first_cell.casefold() in _HEADER_FIRST_CELLS:
            raw_rows.append(
                _raw_row(
                    import_id=import_id,
                    sheet_name=sheet_name,
                    row_number=row_number,
                    values=values_tuple,
                    mode=mode,
                    status=RawRowParseStatus.HEADER,
                    imported_at=imported_at,
                    source_aliases=aliases,
                )
            )
            continue

        raw = _raw_row(
            import_id=import_id,
            sheet_name=sheet_name,
            row_number=row_number,
            values=values_tuple,
            mode=mode,
            status=RawRowParseStatus.VALID,
            imported_at=imported_at,
            source_aliases=aliases,
        )
        parsed_raw, normalized = normalize_portfolio_row(raw)
        raw_rows.append(parsed_raw)
        if normalized is not None:
            positions.append(normalized)

    global_warnings: list[ImportWarning] = []
    if not marker_rows:
        global_warnings.append(
            _blocking(
                ImportWarningCode.MISSING_MARKER,
                f'marker "{MARKER_TEXT}" was not found',
            )
        )
    elif len(marker_rows) > 1:
        global_warnings.append(
            _blocking(
                ImportWarningCode.MULTIPLE_MARKERS,
                f'marker "{MARKER_TEXT}" was found more than once',
            )
        )

    row_warnings = [warning for row in raw_rows for warning in row.warnings]
    warnings = tuple(global_warnings + row_warnings)
    status = (
        ImportPreviewStatus.BLOCKED
        if any(item.severity == ImportWarningSeverity.BLOCKING for item in warnings)
        else ImportPreviewStatus.READY
    )
    aggregated = aggregate_positions(positions)
    total_cost = sum((item.total_cost_basis_usd for item in aggregated), Decimal("0"))
    total_market = sum(
        (item.import_market_value_usd for item in aggregated), Decimal("0")
    )
    daily = sum((item.daily_contribution_krw for item in aggregated), Decimal("0"))
    duplicate_count = sum(1 for item in aggregated if len(item.source_positions) > 1)
    summary = ImportSummary(
        position_count=len(positions),
        unique_ticker_count=len(aggregated),
        duplicate_ticker_count=duplicate_count,
        total_cost_basis_usd=total_cost,
        total_import_market_value_usd=total_market,
        total_import_profit_usd=total_market - total_cost,
        calculated_import_return_percent=(
            (total_market / total_cost - Decimal("1")) * Decimal("100")
            if total_cost > 0
            else None
        ),
        daily_contribution_krw=daily,
        annual_contribution_krw=daily * DEFAULT_TRADING_DAYS_PER_YEAR,
        above_marker_count=sum(
            item.source_input_type == PortfolioInputType.TOTAL_POSITION_VALUE
            for item in positions
        ),
        below_marker_count=sum(
            item.source_input_type == PortfolioInputType.PER_SHARE_PRICE
            for item in positions
        ),
    )
    return ImportPreview(
        import_id=import_id,
        source_file_name=Path(source_file_name).name,
        source_file_sha256=source_file_sha256,
        source_file_size=source_file_size,
        sheet_name=sheet_name,
        imported_at=imported_at.astimezone(timezone.utc),
        status=status,
        marker_rows=tuple(marker_rows),
        raw_rows=tuple(raw_rows),
        normalized_positions=tuple(positions),
        aggregated_positions=aggregated,
        warnings=warnings,
        summary=summary,
    )


def parse_portfolio_xlsx(
    path: str | Path,
    *,
    sheet_name: str | None = None,
    import_id: str | None = None,
    imported_at: datetime | None = None,
    source_aliases: dict[PortfolioInputType, str] | None = None,
) -> ImportPreview:
    workbook_path = Path(path)
    if workbook_path.suffix.casefold() != ".xlsx":
        raise PortfolioImportError("portfolio import requires an .xlsx file")
    size = workbook_path.stat().st_size
    if size > MAX_XLSX_BYTES:
        raise PortfolioImportError("workbook exceeds the import size limit")
    file_hash = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    try:
        with ZipFile(workbook_path) as archive:
            total_uncompressed = sum(item.file_size for item in archive.infolist())
            if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise PortfolioImportError("workbook expands beyond the import size limit")
            selected_sheet, rows = _read_xlsx_rows(archive, sheet_name=sheet_name)
    except BadZipFile:
        raise PortfolioImportError("workbook is not a valid XLSX archive") from None

    return parse_portfolio_rows(
        rows,
        import_id=import_id or str(uuid.uuid4()),
        sheet_name=selected_sheet,
        source_file_name=workbook_path.name,
        source_file_sha256=file_hash,
        source_file_size=size,
        imported_at=imported_at or datetime.now(timezone.utc),
        source_aliases=source_aliases,
    )


def _read_xlsx_rows(
    archive: ZipFile,
    *,
    sheet_name: str | None,
) -> tuple[str, tuple[tuple[int, tuple[str | None, ...]], ...]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    sheets = []
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        name = sheet.attrib["name"]
        relationship_id = sheet.attrib[f"{{{_REL_NS}}}id"]
        sheets.append((name, relationship_id))
    if not sheets:
        raise PortfolioImportError("workbook contains no worksheets")
    selected = next((item for item in sheets if item[0] == sheet_name), None)
    if selected is None and sheet_name is not None:
        raise PortfolioImportError(f"worksheet was not found: {sheet_name}")
    selected = selected or sheets[0]
    target = targets.get(selected[1])
    if target is None:
        raise PortfolioImportError("worksheet relationship is missing")
    worksheet_path = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
    if worksheet_path.startswith("xl/xl/"):
        worksheet_path = worksheet_path[3:]
    shared_strings = _read_shared_strings(archive)
    worksheet = ElementTree.fromstring(archive.read(worksheet_path))
    rows: list[tuple[int, tuple[str | None, ...]]] = []
    for row in worksheet.findall(f".//{{{_MAIN_NS}}}row"):
        row_number = int(row.attrib.get("r", str(len(rows) + 1)))
        cells: dict[int, str | None] = {}
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            reference = cell.attrib.get("r", "A1")
            column_index = _column_index(reference)
            if column_index >= MAX_WORKSHEET_COLUMNS:
                continue
            cells[column_index] = _cell_text(cell, shared_strings)
        width = max(cells, default=-1) + 1
        values = tuple(cells.get(index) for index in range(width))
        rows.append((row_number, values))
    return selected[0], tuple(rows)


def _read_shared_strings(archive: ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return tuple(
        "".join(node.text or "" for node in item.findall(f".//{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    )


def _cell_text(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> str | None:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(
            node.text or "" for node in cell.findall(f".//{{{_MAIN_NS}}}t")
        )
    value = cell.find(f"{{{_MAIN_NS}}}v")
    if value is None or value.text is None:
        return None
    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (IndexError, ValueError):
            raise PortfolioImportError("workbook contains an invalid shared string") from None
    return value.text


def _column_index(reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", reference)
    if match is None:
        raise PortfolioImportError("workbook contains an invalid cell reference")
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1
