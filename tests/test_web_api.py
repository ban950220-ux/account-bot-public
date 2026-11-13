from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient

from account_bot.market import (
    DistributionPoint,
    DistributionSeries,
    EarningsData,
    FundamentalData,
    FxQuote,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    TotalReturnPoint,
    TotalReturnSeries,
    ValuationData,
)
from account_bot.web_api import create_app
from account_bot.web_api.app import main


class _FakePublicMarketProvider(MarketDataProvider):
    name = "test-market"

    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        return PriceQuote(
            market,
            ticker,
            "USD",
            Decimal("30"),
            datetime(2026, 9, 11, tzinfo=timezone.utc),
            self.name,
        )

    async def get_fx(self, base: str, quote: str) -> FxQuote:
        return FxQuote(
            base,
            quote,
            Decimal("1400"),
            datetime(2026, 9, 11, tzinfo=timezone.utc),
            self.name,
        )

    async def get_total_return_series(
        self, market: str, ticker: str, *, lookback_years: int = 10
    ) -> TotalReturnSeries:
        start = date(2021, 9, 13)
        count = 457
        points = tuple(
            TotalReturnPoint(
                start + timedelta(days=index * 4),
                Decimal("100") + Decimal("50") * Decimal(index) / Decimal(count - 1),
            )
            for index in range(count)
        )
        return TotalReturnSeries(
            market,
            ticker,
            "USD",
            datetime(2026, 9, 11, tzinfo=timezone.utc),
            self.name,
            "ADJUSTED_TOTAL_RETURN",
            points[0].trading_date,
            points[-1].trading_date,
            points,
        )

    async def get_distribution_series(
        self, market: str, ticker: str, *, lookback_years: int = 5
    ) -> DistributionSeries:
        return DistributionSeries(
            market,
            ticker,
            "USD",
            datetime(2026, 9, 11, tzinfo=timezone.utc),
            self.name,
            date(2021, 9, 13),
            date(2026, 9, 11),
            (
                DistributionPoint(date(2026, 1, 2), Decimal("0.50")),
                DistributionPoint(date(2026, 7, 2), Decimal("0.50")),
            ),
        )

    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:  # pragma: no cover
        raise NotImplementedError

    async def get_valuation(self, market: str, ticker: str) -> ValuationData:  # pragma: no cover
        raise NotImplementedError

    async def get_earnings(self, market: str, ticker: str) -> EarningsData:  # pragma: no cover
        raise NotImplementedError

    async def get_news(
        self, market: str, ticker: str, *, limit: int = 10
    ) -> list[NewsItem]:  # pragma: no cover
        raise NotImplementedError


def _xlsx_bytes(rows: list[list[str | int | Decimal | None]]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="account-bot-web-test-") as directory:
        path = Path(directory) / "portfolio.xlsx"
        shared: list[str] = []

        def cell_xml(row_number: int, column_number: int, value: object) -> str:
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
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships '
                'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/></Relationships>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>",
            )
            archive.writestr(
                "xl/sharedStrings.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"{shared_xml}</sst>",
            )
        return path.read_bytes()


def _client(database_path: Path) -> TestClient:
    return TestClient(create_app(database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}"))


def test_web_entrypoint_requires_an_explicit_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_BOT_WEB_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="ACCOUNT_BOT_WEB_DATABASE_URL is required"):
        main()


def test_import_commit_portfolio_and_daily_buy_round_trip(tmp_path: Path) -> None:
    workbook = _xlsx_bytes(
        [
            ["종목명", "수량", "매입가", "평가금액", "수익률", "모으기금액"],
            ["AAA", "2", "100", "110", "10", "1000"],
            ["여기 아래는 주당가격"],
            ["AAA", "3", "20", "22", "10", "2000"],
        ]
    )
    with _client(tmp_path / "web.db") as client:
        assert client.get("/api/health").json() == {
            "status": "ok",
            "brokerPolicy": "READ_ONLY",
        }
        empty = client.get("/api/portfolio").json()
        assert empty["dataState"] == "EMPTY"

        preview_response = client.post(
            "/api/import/preview",
            content=workbook,
            headers={"X-Filename": "portfolio.xlsx"},
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["status"] == "READY"
        assert preview["summary"]["position_count"] == 2
        assert preview["summary"]["unique_ticker_count"] == 1
        assert preview["summary"]["total_cost_basis_usd"] == "160"
        assert preview["summary"]["total_import_market_value_usd"] == "176"

        commit = client.post(f"/api/import/{preview['import_id']}/commit")
        assert commit.status_code == 200
        assert commit.json()["normalizedPositionCount"] == 2

        portfolio = client.get("/api/portfolio").json()
        assert portfolio["dataState"] == "READY"
        assert portfolio["priceSource"] == "IMPORT_SNAPSHOT"
        assert portfolio["fx"]["status"] == "UNAVAILABLE"
        assert Decimal(portfolio["summary"]["totalCostBasisUSD"]) == Decimal("160")
        assert Decimal(portfolio["summary"]["totalMarketValueUSD"]) == Decimal("176")
        assert Decimal(portfolio["summary"]["dailyContributionKRW"]) == Decimal("3000")
        assert Decimal(
            portfolio["summary"]["monthlyAverageContributionKRW"]
        ) == Decimal("63000")
        assert Decimal(portfolio["summary"]["annualContributionKRW"]) == Decimal("756000")
        position = portfolio["positions"][0]
        assert Decimal(position["quantity"]) == Decimal("5")
        assert position["sourceCount"] == 2
        assert Decimal(position["averageCostPerShareUSD"]) == Decimal("32")
        assert Decimal(position["calculatedImportReturnPercent"]) == Decimal("10")

        update = client.put(
            "/api/daily-buy/aaa",
            json={"configuredAmountKRW": "5000", "enabled": True},
        )
        assert update.status_code == 200
        assert update.json()["effectiveContributionKRW"] == "5000"
        changed = client.get("/api/portfolio").json()
        assert Decimal(changed["summary"]["dailyContributionKRW"]) == Decimal("5000")
        assert Decimal(changed["summary"]["annualContributionKRW"]) == Decimal("1260000")

        stopped = client.put(
            "/api/daily-buy/AAA",
            json={"configuredAmountKRW": "0", "enabled": False},
        )
        assert stopped.status_code == 200
        assert Decimal(stopped.json()["configuredAmountKRW"]) == Decimal("5000")
        assert stopped.json()["effectiveContributionKRW"] == "0"
        stopped_snapshot = client.get("/api/portfolio").json()
        assert stopped_snapshot["summary"]["dailyContributionKRW"] == "0"
        assert Decimal(
            stopped_snapshot["positions"][0]["configuredContributionKRW"]
        ) == Decimal("5000")


def test_blocked_import_cannot_commit_and_decimal_api_is_strict(tmp_path: Path) -> None:
    workbook = _xlsx_bytes([["AAA", "1", "10", "11", "10", "0"]])
    with _client(tmp_path / "blocked.db") as client:
        preview_response = client.post(
            "/api/import/preview",
            content=workbook,
            headers={"X-Filename": "portfolio.xlsx"},
        )
        preview = preview_response.json()
        assert preview["status"] == "BLOCKED"
        assert client.post(f"/api/import/{preview['import_id']}/commit").status_code == 409
        assert (
            client.put(
                "/api/daily-buy/AAA",
                json={"configuredAmountKRW": 3000, "enabled": True},
            ).status_code
            == 422
        )


def test_import_rejects_non_xlsx_and_empty_body(tmp_path: Path) -> None:
    with _client(tmp_path / "validation.db") as client:
        assert (
            client.post(
                "/api/import/preview",
                content=b"not-an-xlsx",
                headers={"X-Filename": "portfolio.csv"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/import/preview",
                content=b"",
                headers={"X-Filename": "portfolio.xlsx"},
            ).status_code
            == 400
        )


def test_zero_holding_daily_buy_target_is_visible_without_import(tmp_path: Path) -> None:
    with _client(tmp_path / "plan-only.db") as client:
        response = client.put(
            "/api/daily-buy/VT",
            json={"configuredAmountKRW": "3000", "enabled": True},
        )
        assert response.status_code == 200
        snapshot = client.get("/api/portfolio").json()
        assert snapshot["dataState"] == "READY"
        assert snapshot["priceSource"] == "UNAVAILABLE"
        assert snapshot["summary"]["holdingCount"] == 0
        assert snapshot["summary"]["investmentTargetCount"] == 1
        assert Decimal(snapshot["positions"][0]["quantity"]) == Decimal("0")
        assert Decimal(snapshot["positions"][0]["effectiveContributionKRW"]) == Decimal(
            "3000"
        )


def test_live_price_fx_and_total_return_are_applied_without_changing_quantity(tmp_path: Path) -> None:
    workbook = _xlsx_bytes(
        [
            ["종목명", "수량", "매입가", "평가금액", "수익률", "모으기금액"],
            ["AAA", "2", "40", "50", "25", "1000"],
            ["여기 아래는 주당가격"],
        ]
    )
    app = create_app(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'live.db').as_posix()}",
        market_provider=_FakePublicMarketProvider(),
    )
    with TestClient(app) as client:
        preview = client.post(
            "/api/import/preview",
            content=workbook,
            headers={"X-Filename": "portfolio.xlsx"},
        ).json()
        assert client.post(f"/api/import/{preview['import_id']}/commit").status_code == 200
        portfolio = client.get("/api/portfolio?refresh=true").json()
        position = portfolio["positions"][0]
        assert position["quantity"] == "2"
        assert position["importMarketValueUSD"] == "50"
        assert position["latestCurrentPriceUSD"] == "30"
        assert position["currentMarketValueUSD"] == "60"
        assert position["currentMarketValueKRW"] == "84000"
        assert portfolio["summary"]["totalMarketValueKRW"] == "84000"
        assert portfolio["fx"]["status"] == "LIVE"

        performance = client.get("/api/performance?lookbackYears=5").json()
        assert performance["status"] == "READY"
        assert performance["seriesKind"] == "ADJUSTED_TOTAL_RETURN"
        assert Decimal(performance["totalReturnPercent"]) == Decimal("50.0")
        assert performance["points"][0]["index"] == "100"

        projection = client.get("/api/projection").json()
        assert projection["status"] == "READY"
        assert set(projection["horizons"]) == {"1Y", "3Y", "5Y", "7Y", "10Y"}
        assert projection["projectionMethod"] == "HISTORICAL_TOTAL_RETURN_CAGR"
        assert projection["calendar"]["name"] == "US Equity Market Calendar (XNYS)"
        assert len(projection["wealthTimeline"]) >= 120
        assert projection["wealthTimeline"][0]["date"] == projection["asOf"]
        assert len(projection["milestones"]) == 5
        assert projection["milestones"][0]["targetKRW"] == "100000000"
        assert "endingValueKRW" in projection["horizons"]["10Y"]["positions"][0]
        assert projection["income"]["status"] == "READY"
        assert projection["income"]["method"] == "TRAILING_12M_DISTRIBUTION_YIELD"
        assert Decimal(
            projection["income"]["horizons"]["10Y"]["annualDistributionKRW"]
        ) > 0

        comparison = client.post(
            "/api/projection/compare",
            json={
                "changes": [
                    {"ticker": "AAA", "configuredAmountKRW": "2000", "enabled": True}
                ]
            },
        ).json()
        assert comparison["status"] == "READY"
        assert comparison["baseDailyContributionKRW"] == "1000"
        assert comparison["modifiedDailyContributionKRW"] == "2000"
        assert Decimal(comparison["deltas"]["10Y"]["deltaKRW"]) > 0
        assert Decimal(
            comparison["deltas"]["10Y"]["addedPrincipalDeltaKRW"]
        ) > 0
        assert Decimal(
            comparison["deltas"]["10Y"]["investmentGainDeltaKRW"]
        ) > 0
        assert Decimal(
            comparison["deltas"]["10Y"]["annualDistributionDeltaKRW"]
        ) > 0
        modified_projection = comparison["modifiedProjection"]
        assert modified_projection["status"] == "READY"
        assert modified_projection["dailyContributionKRW"] == "2000"
        assert Decimal(
            modified_projection["horizons"]["10Y"]["endingValueKRW"]
        ) == Decimal(comparison["deltas"]["10Y"]["modifiedEndingValueKRW"])
        assert (
            modified_projection["horizons"]["10Y"]["positions"][0]["ticker"]
            == "AAA"
        )
        assert Decimal(
            modified_projection["income"]["horizons"]["10Y"][
                "annualDistributionKRW"
            ]
        ) > 0

        saved_response = client.post(
            "/api/scenarios",
            json={
                "name": "AAA 강화",
                "changes": [
                    {"ticker": "AAA", "configuredAmountKRW": "2000", "enabled": True}
                ],
            },
        )
        assert saved_response.status_code == 201
        saved = saved_response.json()
        assert saved["name"] == "AAA 강화"
        assert saved["dailyContributionKRW"] == "2000"
        assert saved["plans"][0]["configuredAmountKRW"] == "2000"
        scenarios = client.get("/api/scenarios").json()
        assert len(scenarios["scenarios"]) == 1
        saved_comparison = client.get(
            f"/api/scenarios/{saved['id']}/comparison"
        ).json()
        assert saved_comparison["scenario"]["name"] == "AAA 강화"
        assert saved_comparison["modifiedDailyContributionKRW"] == "2000"
        assert Decimal(saved_comparison["deltas"]["10Y"]["deltaKRW"]) > 0
        assert client.delete(f"/api/scenarios/{saved['id']}").status_code == 204
        assert client.get("/api/scenarios").json()["scenarios"] == []

        monte_carlo = client.get(
            "/api/projection/monte-carlo?runs=100&blockDays=20&seed=42"
        ).json()
        assert monte_carlo["status"] == "READY"
        assert monte_carlo["runs"] == 100
        assert monte_carlo["blockDays"] == 20
        assert monte_carlo["seed"] == 42
        assert set(monte_carlo["horizons"]["10Y"]["percentiles"]) == {
            "P10",
            "P25",
            "P50",
            "P75",
            "P90",
        }
        assert Decimal(
            monte_carlo["horizons"]["10Y"]["percentiles"]["P75"][
                "endingValueKRW"
            ]
        ) >= Decimal(
            monte_carlo["horizons"]["10Y"]["percentiles"]["P25"][
                "endingValueKRW"
            ]
        )

        allocation = client.get("/api/allocation").json()
        assert allocation["status"] == "READY"
        assert allocation["tickers"][0]["ticker"] == "AAA"
        assert Decimal(allocation["tickers"][0]["currentPercent"]) == Decimal("100")
        assert Decimal(allocation["tickers"][0]["projected10YPercent"]) == Decimal(
            "100"
        )

        detail = client.get("/api/etf/AAA").json()
        assert detail["status"] == "READY"
        assert detail["ticker"] == "AAA"
        assert detail["position"]["quantity"] == "2"
        assert Decimal(detail["portfolioWeightPercent"]) == Decimal("100")
        assert Decimal(detail["history"]["return1YPercent"]) > 0
        assert detail["history"]["cagr3YPercent"] is not None
        assert detail["history"]["annualizedVolatilityPercent"] is not None
        assert detail["distribution"]["points"][0]["classification"] == (
            "UNCLASSIFIED_DISTRIBUTION"
        )
        assert set(detail["projection"]["horizons"]) == {
            "1Y",
            "3Y",
            "5Y",
            "7Y",
            "10Y",
        }
        assert Decimal(
            detail["projection"]["horizons"]["10Y"]["annualDistributionKRW"]
        ) > 0
