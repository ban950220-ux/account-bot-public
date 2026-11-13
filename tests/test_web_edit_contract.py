"""Diagnostic contract for the separate localhost dashboard, using disposable data only."""

from pathlib import Path

from fastapi.testclient import TestClient

from account_bot.web_api import create_app


def test_local_dashboard_edit_and_scenario_contract(tmp_path: Path) -> None:
    database = tmp_path / "web-contract.db"
    app = create_app(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    with TestClient(app) as client:
        # The preview is a draft: it never persists an ETF plan.
        preview = client.post(
            "/api/projection/compare",
            json={"changes": [{"ticker": "FIX", "configuredAmountKRW": "3000", "enabled": True}]},
        )
        assert preview.status_code == 200
        assert client.get("/api/portfolio").json()["summary"]["investmentTargetCount"] == 0

        added = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "3000", "enabled": True}
        )
        assert added.status_code == 200
        assert added.json()["version"] == 1
        # Identical double click is idempotent in the repository.
        replay = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "3000", "enabled": True}
        )
        assert replay.status_code == 200
        assert replay.json()["version"] == 1

        off = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "0", "enabled": False}
        )
        assert off.status_code == 200
        assert off.json()["configuredAmountKRW"] == "3000.00000000"
        assert off.json()["effectiveContributionKRW"] == "0"
        assert off.json()["version"] == 2
        assert client.get("/api/portfolio").json()["summary"]["dailyContributionKRW"] == "0"
        assert client.put(
            "/api/daily-buy/NEW", json={"configuredAmountKRW": "0", "enabled": False}
        ).status_code == 422

        saved = client.post(
            "/api/scenarios",
            json={"name": "fixture", "changes": [
                {"ticker": "FIX", "configuredAmountKRW": "5000", "enabled": True}
            ]},
        )
        assert saved.status_code == 201
        scenario_id = saved.json()["id"]
        assert saved.json()["plans"][0]["configuredAmountKRW"] == "5000"
        duplicate_click = client.post(
            "/api/scenarios",
            json={"name": "fixture", "changes": [
                {"ticker": "FIX", "configuredAmountKRW": "5000", "enabled": True}
            ]},
        )
        assert duplicate_click.status_code == 201
        assert duplicate_click.json()["id"] == scenario_id
        assert len(client.get("/api/scenarios").json()["scenarios"]) == 1
        # A scenario is separate from the current plan; saving it does not turn the plan on.
        assert client.get("/api/portfolio").json()["summary"]["dailyContributionKRW"] == "0"
        assert client.get(f"/api/scenarios/{scenario_id}/comparison").status_code == 200
        assert client.delete(f"/api/scenarios/{scenario_id}").status_code == 204
        assert client.get("/api/scenarios").json()["scenarios"] == []
        assert client.delete(f"/api/scenarios/{scenario_id}").status_code == 404


def test_local_dashboard_stale_save_has_no_version_precondition(tmp_path: Path) -> None:
    database = tmp_path / "stale-contract.db"
    app = create_app(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    with TestClient(app) as client:
        first = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "1000", "enabled": True}
        )
        assert first.json()["version"] == 1
        newer = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "2000", "enabled": True}
        )
        assert newer.json()["version"] == 2
        # The current HTTP request has no expectedVersion field; an old screen can overwrite it.
        stale_screen = client.put(
            "/api/daily-buy/FIX", json={"configuredAmountKRW": "1500", "enabled": True}
        )
        assert stale_screen.status_code == 200
        assert stale_screen.json()["version"] == 3
        position = client.get("/api/portfolio").json()["positions"][0]
        assert position["configuredContributionKRW"] == "1500.00000000"
