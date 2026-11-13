"""Persistence for workbook import previews in the local SQLite database."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from account_bot.storage import (
    Database,
    NormalizedPortfolioImportPosition,
    PortfolioImportBatch,
    RawPortfolioImportRow,
)

from .models import ImportPreview


@dataclass(frozen=True, slots=True)
class StoredImportCounts:
    import_id: int
    raw_rows: int
    normalized_positions: int


class PortfolioImportStore:
    """Store immutable raw and normalized snapshots; aggregates stay derived."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_preview(
        self,
        preview: ImportPreview,
        *,
        portfolio_id: int | None = None,
    ) -> StoredImportCounts:
        async with self.database.transaction() as session:
            existing = await session.scalar(
                select(PortfolioImportBatch).where(
                    PortfolioImportBatch.import_key == preview.import_id
                )
            )
            if existing is not None:
                return await self._counts(session, existing.id)

            batch = PortfolioImportBatch(
                import_key=preview.import_id,
                portfolio_id=portfolio_id,
                source_file_name=preview.source_file_name,
                source_file_sha256=preview.source_file_sha256,
                source_file_size=preview.source_file_size,
                sheet_name=preview.sheet_name,
                status=preview.status.value,
                marker_count=len(preview.marker_rows),
                marker_rows=list(preview.marker_rows),
                position_count=preview.summary.position_count,
                unique_ticker_count=preview.summary.unique_ticker_count,
                duplicate_ticker_count=preview.summary.duplicate_ticker_count,
                total_cost_basis_usd=preview.summary.total_cost_basis_usd,
                total_import_market_value_usd=(
                    preview.summary.total_import_market_value_usd
                ),
                total_import_profit_usd=preview.summary.total_import_profit_usd,
                calculated_import_return_percent=(
                    preview.summary.calculated_import_return_percent
                ),
                daily_contribution_krw=preview.summary.daily_contribution_krw,
                warning_codes=[warning.code.value for warning in preview.warnings],
                imported_at=preview.imported_at,
            )
            session.add(batch)
            await session.flush()

            stored_rows: dict[int, RawPortfolioImportRow] = {}
            for row in preview.raw_rows:
                record = RawPortfolioImportRow(
                    import_id=batch.id,
                    sheet_name=row.sheet_name,
                    row_number=row.row_number,
                    raw_ticker=row.raw_ticker,
                    raw_quantity=row.raw_quantity,
                    raw_purchase_value=row.raw_purchase_value,
                    raw_market_value=row.raw_market_value,
                    raw_provided_return_percent=row.raw_provided_return_percent,
                    raw_daily_contribution_krw=row.raw_daily_contribution_krw,
                    source_section_id=row.source_section_id,
                    source_account_alias=row.source_account_alias,
                    source_input_type=(
                        row.detected_input_type.value if row.detected_input_type else None
                    ),
                    parse_status=row.parse_status.value,
                    raw_cells=list(row.raw_cells),
                    warning_codes=[warning.code.value for warning in row.warnings],
                    imported_at=row.imported_at,
                )
                session.add(record)
                stored_rows[row.row_number] = record
            await session.flush()

            for position in preview.normalized_positions:
                raw_record = stored_rows[position.source_row_number]
                session.add(
                    NormalizedPortfolioImportPosition(
                        import_id=batch.id,
                        raw_row_id=raw_record.id,
                        market=position.instrument.market,
                        ticker=position.instrument.ticker,
                        asset_type=position.instrument.asset_type.value,
                        currency=position.instrument.currency.value,
                        quantity=position.quantity,
                        total_cost_basis_usd=position.total_cost_basis_usd,
                        import_market_value_usd=position.import_market_value_usd,
                        average_cost_per_share_usd=position.average_cost_per_share_usd,
                        import_current_price_usd=position.import_current_price_usd,
                        import_profit_usd=position.import_profit_usd,
                        calculated_import_return_percent=(
                            position.calculated_import_return_percent
                        ),
                        provided_return_percent=position.provided_return_percent,
                        daily_contribution_krw=position.daily_contribution_krw,
                        source_section_id=position.source_section_id,
                        source_account_alias=position.source_account_alias,
                        source_input_type=position.source_input_type.value,
                        source_row_number=position.source_row_number,
                        warning_codes=[warning.code.value for warning in position.warnings],
                    )
                )
            await session.flush()
            return StoredImportCounts(
                import_id=batch.id,
                raw_rows=len(preview.raw_rows),
                normalized_positions=len(preview.normalized_positions),
            )

    @staticmethod
    async def _counts(session, import_id: int) -> StoredImportCounts:
        raw_count = await session.scalar(
            select(func.count())
            .select_from(RawPortfolioImportRow)
            .where(RawPortfolioImportRow.import_id == import_id)
        )
        normalized_count = await session.scalar(
            select(func.count())
            .select_from(NormalizedPortfolioImportPosition)
            .where(NormalizedPortfolioImportPosition.import_id == import_id)
        )
        return StoredImportCounts(
            import_id=import_id,
            raw_rows=int(raw_count or 0),
            normalized_positions=int(normalized_count or 0),
        )
