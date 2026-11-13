"""Redaction-safe live web provider smoke test using a temporary database."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from account_bot.market import YFinanceProvider
from account_bot.web_api import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="account-bot-web-live-") as directory:
        database_path = Path(directory) / "live-smoke.db"
        app = create_app(
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            market_provider=YFinanceProvider(),
        )
        with TestClient(app) as client:
            preview = client.post(
                "/api/import/preview?sheetName=Sheet1",
                content=args.workbook.read_bytes(),
                headers={"X-Filename": "portfolio.xlsx"},
            ).json()
            commit = client.post(f"/api/import/{preview['import_id']}/commit")
            commit.raise_for_status()
            portfolio = client.get("/api/portfolio?refresh=true", timeout=180).json()
            print(
                "portfolio",
                portfolio["priceSource"],
                "fx",
                portfolio["fx"]["status"],
                "live_prices",
                sum(item["priceStatus"] == "LIVE" for item in portfolio["positions"]),
                "positions",
                len(portfolio["positions"]),
            )
            performance = client.get(
                "/api/performance?lookbackYears=5&refresh=true", timeout=300
            ).json()
            print(
                "performance",
                performance["status"],
                "points",
                len(performance["points"]),
                "coverage",
                performance.get("dataCoveragePercent"),
                "series",
                performance.get("seriesKind"),
            )
            projection = client.get("/api/projection").json()
            print(
                "projection",
                projection["status"],
                "horizons",
                len(projection.get("horizons", {})),
                "coverage",
                projection.get("dataCoveragePercent"),
                "calendar",
                projection.get("calendar", {}).get("name"),
                "income",
                projection.get("income", {}).get("status"),
                "income_coverage",
                projection.get("income", {}).get("dataCoveragePercent"),
            )


if __name__ == "__main__":
    main()
