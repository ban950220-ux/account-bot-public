"""FastAPI application for the local personal portfolio dashboard."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from account_bot.market import CachedMarketDataProvider, MarketDataProvider, YFinanceProvider
from account_bot.portfolio_import import (
    ImportPreview,
    PortfolioImportError,
    PortfolioImportStore,
    parse_portfolio_xlsx,
)
from account_bot.projection import TradingCalendarProvider, XNYSTradingCalendar
from account_bot.storage import Database, SQLitePortfolioRepository

from .market_service import WebMarketDataService
from .portfolio_service import PortfolioWebService

MAX_PREVIEWS = 8


class DailyBuyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configuredAmountKRW: str
    enabled: bool


class ScenarioChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str
    configuredAmountKRW: str
    enabled: bool


class ScenarioPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changes: list[ScenarioChange]


class SavedScenarioCreate(ScenarioPreview):
    name: str


class PreviewRegistry:
    def __init__(self) -> None:
        self._items: OrderedDict[str, ImportPreview] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(self, preview: ImportPreview) -> None:
        async with self._lock:
            self._items[preview.import_id] = preview
            self._items.move_to_end(preview.import_id)
            while len(self._items) > MAX_PREVIEWS:
                self._items.popitem(last=False)

    async def get(self, import_id: str) -> ImportPreview | None:
        async with self._lock:
            return self._items.get(import_id)


def create_app(
    *,
    database_url: str,
    market_provider: MarketDataProvider | None = None,
    trading_calendar: TradingCalendarProvider | None = None,
) -> FastAPI:
    database = Database(database_url, sqlite_busy_timeout_ms=5_000)
    repository = SQLitePortfolioRepository(database)
    import_store = PortfolioImportStore(database)
    market_data = WebMarketDataService(repository, market_provider)
    if trading_calendar is None and market_provider is not None:
        trading_calendar = XNYSTradingCalendar()
    portfolio_service = PortfolioWebService(
        database, repository, market_data, trading_calendar
    )
    previews = PreviewRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await database.initialize()
        await repository.initialize_default_portfolio()
        try:
            yield
        finally:
            await database.dispose()

    app = FastAPI(
        title="ETF 장기투자 대시보드 API",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Filename"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "brokerPolicy": "READ_ONLY"}

    @app.post("/api/import/preview")
    async def preview_import(
        request: Request,
        filename: Annotated[str, Header(alias="X-Filename")] = "portfolio.xlsx",
        sheet_name: Annotated[str | None, Query(alias="sheetName")] = None,
    ) -> dict:
        body = await request.body()
        if not body:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "workbook body is empty")
        if len(body) > 20 * 1024 * 1024:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "workbook is too large")
        safe_name = Path(filename).name
        if not safe_name.casefold().endswith(".xlsx"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "an .xlsx workbook is required")
        try:
            with tempfile.TemporaryDirectory(prefix="account-bot-import-") as directory:
                workbook = Path(directory) / safe_name
                workbook.write_bytes(body)
                preview = parse_portfolio_xlsx(workbook, sheet_name=sheet_name)
        except (OSError, PortfolioImportError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        await previews.put(preview)
        return preview.to_api_dict()

    @app.post("/api/import/{import_id}/commit")
    async def commit_import(import_id: str) -> dict[str, object]:
        preview = await previews.get(import_id)
        if preview is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "import preview was not found")
        if not preview.can_commit:
            raise HTTPException(status.HTTP_409_CONFLICT, "blocked import cannot be committed")
        counts = await import_store.save_preview(preview)
        return {
            "status": "COMMITTED",
            "importId": import_id,
            "rawRowCount": counts.raw_rows,
            "normalizedPositionCount": counts.normalized_positions,
        }

    @app.get("/api/portfolio")
    async def portfolio(
        refresh: Annotated[bool, Query()] = False,
    ) -> dict:
        return await portfolio_service.get_snapshot(refresh=refresh)

    @app.get("/api/performance")
    async def performance(
        lookback_years: Annotated[int, Query(alias="lookbackYears")] = 10,
        refresh: Annotated[bool, Query()] = False,
    ) -> dict:
        try:
            return await portfolio_service.get_performance(
                lookback_years=lookback_years,
                refresh=refresh,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    @app.get("/api/projection")
    async def projection(
        refresh: Annotated[bool, Query()] = False,
    ) -> dict:
        return await portfolio_service.get_projection(refresh=refresh)

    @app.post("/api/projection/compare")
    async def compare_projection(payload: Annotated[ScenarioPreview, Body()]) -> dict:
        changes: dict[str, tuple[Decimal, bool]] = {}
        for item in payload.changes:
            ticker = item.ticker.strip().upper()
            if not ticker or len(ticker) > 32:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ticker is malformed")
            try:
                amount = Decimal(item.configuredAmountKRW)
            except (InvalidOperation, ValueError):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "configuredAmountKRW must be a decimal string",
                ) from None
            if not amount.is_finite() or amount < 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "configuredAmountKRW must be finite and nonnegative",
                )
            changes[ticker] = (amount, item.enabled)
        return await portfolio_service.compare_projection(changes)

    @app.get("/api/scenarios")
    async def list_scenarios() -> dict:
        return await portfolio_service.list_scenarios()

    @app.post("/api/scenarios", status_code=status.HTTP_201_CREATED)
    async def save_scenario(payload: Annotated[SavedScenarioCreate, Body()]) -> dict:
        changes: dict[str, tuple[Decimal, bool]] = {}
        for item in payload.changes:
            ticker = item.ticker.strip().upper()
            try:
                amount = Decimal(item.configuredAmountKRW)
            except (InvalidOperation, ValueError):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "configuredAmountKRW must be a decimal string",
                ) from None
            if not amount.is_finite() or amount < 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "configuredAmountKRW must be finite and nonnegative",
                )
            changes[ticker] = (amount, item.enabled)
        try:
            return await portfolio_service.save_scenario(
                name=payload.name,
                changes=changes,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    @app.get("/api/scenarios/{scenario_id}/comparison")
    async def compare_saved_scenario(scenario_id: int) -> dict:
        try:
            return await portfolio_service.compare_saved_scenario(scenario_id)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @app.delete("/api/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_saved_scenario(scenario_id: int) -> None:
        try:
            await portfolio_service.delete_saved_scenario(scenario_id)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @app.get("/api/projection/monte-carlo")
    async def monte_carlo_projection(
        runs: Annotated[int, Query(ge=100, le=5000)] = 1000,
        block_days: Annotated[int, Query(alias="blockDays", ge=5, le=60)] = 20,
        seed: Annotated[int, Query(ge=0, le=2_147_483_647)] = 20260912,
        refresh: Annotated[bool, Query()] = False,
    ) -> dict:
        return await portfolio_service.get_monte_carlo_projection(
            runs=runs,
            block_days=block_days,
            seed=seed,
            refresh=refresh,
        )

    @app.get("/api/allocation")
    async def allocation() -> dict:
        return await portfolio_service.get_allocation()

    @app.get("/api/etf/{ticker}")
    async def etf_detail(ticker: str) -> dict:
        try:
            return await portfolio_service.get_etf_detail(ticker)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    @app.put("/api/daily-buy/{ticker}")
    async def update_daily_buy(
        ticker: str,
        payload: Annotated[DailyBuyUpdate, Body()],
    ) -> dict[str, object]:
        try:
            return await portfolio_service.update_daily_buy(
                ticker,
                configured_amount_krw=payload.configuredAmountKRW,
                enabled=payload.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    return app


def main() -> None:
    import uvicorn

    database_url = os.environ.get("ACCOUNT_BOT_WEB_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit(
            "ACCOUNT_BOT_WEB_DATABASE_URL is required; point it at an explicit local SQLite DB"
        )
    live_enabled = os.environ.get("ACCOUNT_BOT_WEB_LIVE_MARKET_DATA", "1").strip().lower()
    provider = (
        CachedMarketDataProvider(YFinanceProvider())
        if live_enabled not in {"0", "false", "no", "off"}
        else None
    )
    uvicorn.run(
        create_app(database_url=database_url, market_provider=provider),
        host="127.0.0.1",
        port=8000,
    )
