"""Immutable domain models for local portfolio workbook imports."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from account_bot.domain import AssetType, Currency, InstrumentKey, as_decimal


class PortfolioInputType(str, Enum):
    TOTAL_POSITION_VALUE = "TOTAL_POSITION_VALUE"
    PER_SHARE_PRICE = "PER_SHARE_PRICE"


class RawRowParseStatus(str, Enum):
    BLANK = "BLANK"
    HEADER = "HEADER"
    MARKER = "MARKER"
    VALID = "VALID"
    INVALID = "INVALID"


class ImportPreviewStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class ImportWarningSeverity(str, Enum):
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class ImportWarningCode(str, Enum):
    MISSING_MARKER = "MISSING_MARKER"
    MULTIPLE_MARKERS = "MULTIPLE_MARKERS"
    INVALID_TICKER = "INVALID_TICKER"
    INVALID_NUMERIC = "INVALID_NUMERIC"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
    NONPOSITIVE_COST_BASIS = "NONPOSITIVE_COST_BASIS"
    NEGATIVE_MARKET_VALUE = "NEGATIVE_MARKET_VALUE"
    NEGATIVE_CONTRIBUTION = "NEGATIVE_CONTRIBUTION"
    ZERO_QUANTITY_WITH_VALUE = "ZERO_QUANTITY_WITH_VALUE"
    ZERO_QUANTITY_WITHOUT_PLAN = "ZERO_QUANTITY_WITHOUT_PLAN"
    SOURCE_RETURN_MISMATCH = "SOURCE_RETURN_MISMATCH"
    SOURCE_SECTION_MISMATCH = "SOURCE_SECTION_MISMATCH"


@dataclass(frozen=True, slots=True)
class ImportWarning:
    code: ImportWarningCode
    severity: ImportWarningSeverity
    message: str
    row_number: int | None = None
    ticker: str | None = None


@dataclass(frozen=True, slots=True)
class RawPortfolioRow:
    import_id: str
    sheet_name: str
    row_number: int
    raw_ticker: str | None
    raw_quantity: str | None
    raw_purchase_value: str | None
    raw_market_value: str | None
    raw_provided_return_percent: str | None
    raw_daily_contribution_krw: str | None
    source_section_id: str | None
    source_account_alias: str | None
    detected_input_type: PortfolioInputType | None
    parse_status: RawRowParseStatus
    raw_cells: tuple[str | None, ...]
    imported_at: datetime
    warnings: tuple[ImportWarning, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    instrument: InstrumentKey
    quantity: Decimal
    total_cost_basis_usd: Decimal
    import_market_value_usd: Decimal
    import_current_price_usd: Decimal | None
    average_cost_per_share_usd: Decimal | None
    import_profit_usd: Decimal
    calculated_import_return_percent: Decimal | None
    provided_return_percent: Decimal | None
    daily_contribution_krw: Decimal
    source_account_alias: str
    source_input_type: PortfolioInputType
    source_import_id: str
    source_row_number: int
    source_section_id: str
    warnings: tuple[ImportWarning, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in (
            "quantity",
            "total_cost_basis_usd",
            "import_market_value_usd",
            "import_profit_usd",
            "daily_contribution_krw",
        ):
            object.__setattr__(
                self,
                field_name,
                as_decimal(getattr(self, field_name), name=field_name),
            )
        for field_name in (
            "import_current_price_usd",
            "average_cost_per_share_usd",
            "calculated_import_return_percent",
            "provided_return_percent",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, as_decimal(value, name=field_name))


@dataclass(frozen=True, slots=True)
class AggregatedPosition:
    instrument: InstrumentKey
    quantity: Decimal
    total_cost_basis_usd: Decimal
    import_market_value_usd: Decimal
    average_cost_per_share_usd: Decimal | None
    import_current_price_usd: Decimal | None
    import_profit_usd: Decimal
    calculated_import_return_percent: Decimal | None
    daily_contribution_krw: Decimal
    source_positions: tuple[NormalizedPosition, ...]
    warnings: tuple[ImportWarning, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    position_count: int
    unique_ticker_count: int
    duplicate_ticker_count: int
    total_cost_basis_usd: Decimal
    total_import_market_value_usd: Decimal
    total_import_profit_usd: Decimal
    calculated_import_return_percent: Decimal | None
    daily_contribution_krw: Decimal
    annual_contribution_krw: Decimal
    above_marker_count: int
    below_marker_count: int


@dataclass(frozen=True, slots=True)
class ImportPreview:
    import_id: str
    source_file_name: str
    source_file_sha256: str
    source_file_size: int
    sheet_name: str
    imported_at: datetime
    status: ImportPreviewStatus
    marker_rows: tuple[int, ...]
    raw_rows: tuple[RawPortfolioRow, ...]
    normalized_positions: tuple[NormalizedPosition, ...]
    aggregated_positions: tuple[AggregatedPosition, ...]
    warnings: tuple[ImportWarning, ...]
    summary: ImportSummary

    @property
    def can_commit(self) -> bool:
        return self.status == ImportPreviewStatus.READY

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize Decimal values as strings for a future HTTP boundary."""

        return _api_value(self)


def us_etf(ticker: str) -> InstrumentKey:
    return InstrumentKey("US", ticker, AssetType.ETF, Currency.USD)


def _api_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, InstrumentKey):
        return {
            "market": value.market,
            "ticker": value.ticker,
            "assetType": value.asset_type.value,
            "currency": value.currency.value,
        }
    if is_dataclass(value):
        return {
            item.name: _api_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _api_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_api_value(item) for item in value]
    return value
