"""Synthetic CPython CPU probe; not a Cloudflare Workers limit test."""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from account_bot.cloud_webhook import admitted_update_id  # noqa: E402
from account_bot.domain import BrokerResult, Currency, InstrumentKey, Position  # noqa: E402
from account_bot.portfolio import PortfolioAggregator  # noqa: E402
from account_bot.reports import build_periodic_report  # noqa: E402
from account_bot.services import PortfolioRefresh  # noqa: E402


def _refresh() -> PortfolioRefresh:
    source = BrokerResult.ok(
        "fixture", (
            Position(
                "fixture", "synthetic",
                InstrumentKey("KRX", "FIXTURE", currency=Currency.KRW),
                "1", market_value="1000",
            ),
        ),
    )
    return PortfolioRefresh(
        snapshot=PortfolioAggregator(Currency.KRW).aggregate((source,)),
        account_results=(), position_results=(source,), cash_results=(),
        fx_quote=None, collected_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def _probe(label: str, action, *, iterations: int = 200) -> None:
    samples = []
    for _ in range(iterations):
        start = time.process_time_ns()
        action()
        samples.append((time.process_time_ns() - start) / 1_000_000)
    p95 = sorted(samples)[int(0.95 * len(samples)) - 1]
    print(f"{label}: median={statistics.median(samples):.4f}ms p95={p95:.4f}ms")


def main() -> None:
    body = json.dumps({
        "update_id": 1,
        "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}},
    }).encode()
    refresh = _refresh()
    _probe(
        "webhook admission",
        lambda: admitted_update_id(
            {"X-Telegram-Bot-Api-Secret-Token": "fixture"}, body,
            expected_secret="fixture", allowed_user_ids={42},
        ),
    )
    _probe("daily report composition", lambda: build_periodic_report("daily", refresh))
    _probe("weekly report composition", lambda: build_periodic_report("weekly", refresh))
    print("CPython fixture only; excludes D1, broker/Telegram HTTP, Pyodide, and Worker startup")


if __name__ == "__main__":
    main()
