"""Compare a dependency-free JS Worker candidate against Python with synthetic input."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from account_bot.analysis.rebalance import TargetAllocation, suggest_new_money_rebalance
from account_bot.cloud_schedule import due_jobs
from account_bot.cloud_webhook import admitted_update_id
from account_bot.domain import (
    Account,
    BrokerError,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    FxQuote,
    InstrumentKey,
    Money,
    Position,
    Transaction,
)
from account_bot.portfolio import PortfolioAggregator, RecurringBuyPlan, evaluate_recurring_buy
from account_bot.reports import (
    build_daily_report,
    build_weekly_report,
    format_accounts,
    format_assets,
    format_portfolio,
    format_recurring,
)
from account_bot.reports.formatter import format_rebalance
from account_bot.services import PortfolioRefresh
from account_bot.telegram_ui.handlers import TelegramCommandController

ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-secret"
HEADER = [["X-Telegram-Bot-Api-Secret-Token", SECRET]]
MONDAY = "2026-10-26"


def _money(value: Decimal, places: int) -> str:
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{rounded:,.{places}f}"


def _plan(*, enabled: bool = True, weekdays: list[int] | None = None) -> dict:
    return {
        "instrument": {"market": "US", "ticker": "TEST", "assetType": "ETF", "currency": "KRW"},
        "dailyAmount": "100.00", "currency": "KRW", "enabled": enabled,
        "broker": "synthetic", "accountId": "synthetic-account",
        "scheduledWeekdays": weekdays if weekdays is not None else [0], "startedAt": None,
    }


def _transaction(amount: str, *, side: str = "BUY", market: str = "NYSE",
                 broker: str = "synthetic", account: str = "synthetic-account") -> dict:
    return {
        "transactionId": f"fixture-{amount}-{side}-{market}-{broker}-{account}",
        "broker": broker, "accountId": account,
        "instrument": {"market": market, "ticker": "TEST", "assetType": "ETF", "currency": "KRW"},
        "side": side, "tradeDate": MONDAY, "grossAmount": amount, "currency": "KRW",
    }


def _position(broker: str, *, market: str = "US", ticker: str = "TEST",
              currency: str = "USD", market_value: str | None = "10.5",
              cost_basis: str | None = "8", quantity: str = "1") -> dict:
    return {
        "broker": broker, "accountId": f"{broker}-fixture",
        "instrument": {"market": market, "ticker": ticker,
                       "assetType": "ETF", "currency": currency},
        "quantity": quantity, "marketValue": market_value, "costBasis": cost_basis,
    }


def _result(broker: str, status: str = "ok", *, items: list[dict] | None = None) -> dict:
    return {"broker": broker, "status": status, "items": items or []}


def _fixtures() -> dict:
    generator = random.Random(741)
    division_cases = [["1", "3"], ["1600", "15"], ["2925000", "3155000"],
                      ["-1", "8"], ["0", "7"], ["1", "2"]]
    for _ in range(96):
        left = Decimal(generator.randrange(-10**12, 10**12)).scaleb(-generator.randrange(7))
        right = Decimal(generator.randrange(1, 10**12)).scaleb(-generator.randrange(7))
        division_cases.append([str(left), str(right)])
    update = {"update_id": 73, "message": {
        "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
    }}
    options = {"targetDate": MONDAY, "isTradingDay": True,
               "dataComplete": True, "cutoffPassed": True,
               "tolerancePct": "1", "absoluteTolerance": "0"}
    return {
        "money": [["1.005", 2], ["-1.005", 2], ["2.5", 0],
                  ["-2.5", 0], ["9007199254740993.01", 2]],
        "divisions": division_cases,
        "budgets": [
            [[{"enabled": True, "dailyAmount": "2000", "currency": "KRW",
               "scheduledWeekdays": [0]}], None],
            [[{"enabled": True, "dailyAmount": "10.25", "currency": "USD",
               "scheduledWeekdays": [0, 1, 2, 3, 4]},
              {"enabled": False, "dailyAmount": "999", "currency": "KRW",
               "scheduledWeekdays": [0, 1, 2, 3, 4]}], "1400.1234"],
            [[{"enabled": True, "dailyAmount": "10", "currency": "USD",
               "scheduledWeekdays": [0, 1]}], None],
        ],
        "schedules": [
            ["2026-10-25T23:00:00Z", "Asia/Seoul", "08:00", "09:00", 5],
            ["2026-10-30T23:30:00Z", "Asia/Seoul", "08:00", "09:00", 5],
            ["2026-10-31T00:00:00Z", "Asia/Seoul", "08:00", "09:00", 5],
            ["2026-11-02T13:30:00Z", "America/New_York", "08:30", "09:00", 5],
        ],
        "admissions": [
            {"headers": HEADER, "update": update, "secret": SECRET, "allowed": [42]},
            {"headers": HEADER, "update": update, "secret": SECRET, "allowed": [43]},
            {"headers": [[HEADER[0][0], "wrong"]], "update": update,
             "secret": SECRET, "allowed": [42]},
            {"headers": HEADER, "update": {"update_id": 74, "message": {
                "from": {"id": 42}, "chat": {"id": 42, "type": "group"}}},
             "secret": SECRET, "allowed": [42]},
        ],
        "ledgers": [
            {"plan": _plan(), "transactions": [_transaction("99.00")], "options": options},
            {"plan": _plan(), "transactions": [_transaction("50.00")], "options": options},
            {"plan": _plan(), "transactions": [_transaction("50.00")],
             "options": {**options, "cutoffPassed": False}},
            {"plan": _plan(), "transactions": [], "options": options},
            {"plan": _plan(), "transactions": [_transaction("50.00")],
             "options": {**options, "dataComplete": False}},
            {"plan": _plan(enabled=False), "transactions": [], "options": options},
            {"plan": _plan(weekdays=[4]), "transactions": [], "options": options},
            {"plan": _plan(), "transactions": [
                _transaction("80.00", side="SELL"), _transaction("70.00", broker="other"),
                _transaction("60.00", account="other")], "options": options},
        ],
        "aggregations": [
            {"baseCurrency": "KRW", "expectedBrokers": [" KIWOOM_MAIN "],
             "fxQuotes": [{"base": "USD", "quote": "KRW", "rate": "1400.25",
                           "isStale": False}],
             "positionResults": [_result("kiwoom_main", items=[_position("kiwoom_main")])],
             "cashResults": [_result("kiwoom_main", items=[{
                 "broker": "kiwoom_main", "accountId": "fixture", "currency": "KRW",
                 "amount": "100.01"}])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main", "toss"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main", market="NASDAQ")]),
                 _result("toss", "partial", items=[_position("toss")])]},
            {"baseCurrency": "KRW", "expectedBrokers": ["kiwoom_main"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main")])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main", cost_basis=None)])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main", "meritz"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main")])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main", market="NASDAQ"),
                 _position("kiwoom_main", market="US", market_value=None)])]},
            {"baseCurrency": "KRW", "expectedBrokers": ["kiwoom_main"],
             "fxQuotes": [{"base": "USD", "quote": "KRW", "rate": "1400",
                           "isStale": True}],
             "positionResults": [_result("kiwoom_main", "stale", items=[
                 _position("kiwoom_main")])],
             "cashResults": [_result("kiwoom_main", items=[{
                 "broker": "kiwoom_main", "accountId": "fixture", "currency": "USD",
                 "amount": "2.50"}])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main", "toss"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main")]), _result("toss", "unavailable")],
             "cashResults": [_result("kiwoom_main", items=[{
                 "broker": "kiwoom_main", "accountId": "fixture", "currency": "USD",
                 "amount": "1"}])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main"],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main", market="NASDAQ", quantity="3",
                           market_value="11", cost_basis="10"),
                 _position("kiwoom_main", market="NYSE", quantity="1",
                           market_value="5", cost_basis="4"),
                 _position("kiwoom_main", market="US", quantity="1",
                           market_value="6", cost_basis="5")])]},
            {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main"],
             "fxQuotes": [{"base": "USD", "quote": "KRW", "rate": "1400",
                           "isStale": False}],
             "positionResults": [_result("kiwoom_main", items=[
                 _position("kiwoom_main", market="KRX", currency="KRW",
                           market_value="1000", cost_basis="800")])]},
        ],
        "accountReports": [
            {"isComplete": True, "results": [{"broker": "kiwoom_main", "status": "ok",
                "items": [{"accountId": "synthetic-account", "displayName": "***fixture"}]}]},
            {"isComplete": False, "results": [{"broker": "meritz", "status": "stale",
                "items": []}]},
        ],
        "stockCases": [
            {"raw": "TEST", "providerConfigured": True, "quote": {
                "market": "NASDAQ", "ticker": "TEST", "price": "10.255",
                "currency": "USD", "asOf": "2026-02-02T01:00:00Z",
                "provider": "fixture", "stale": False}},
            {"raw": "KOSPI:005930", "providerConfigured": True, "quote": {
                "market": "KOSPI", "ticker": "005930", "price": "13000.5",
                "currency": "KRW", "asOf": "2026-02-02T01:00:00Z",
                "provider": "fixture", "stale": True}},
            {"raw": "TEST", "providerConfigured": True, "quote": None},
            {"raw": "TEST", "providerConfigured": False, "quote": None},
            {"raw": "", "providerConfigured": False, "quote": None},
        ],
    }


def test_js_candidate_matches_python_for_synthetic_boundaries() -> None:
    fixture = _fixtures()
    result = subprocess.run(
        ["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
        input=json.dumps(fixture), encoding="utf-8", capture_output=True, check=True,
    )
    js = json.loads(result.stdout)
    async def static_reply(name: str) -> str:
        class Message:
            def __init__(self) -> None:
                self.chunks: list[str] = []

            async def reply_text(self, value: str, **_kwargs: object) -> None:
                self.chunks.append(value)

        message = Message()
        controller = TelegramCommandController(settings=None, portfolios=None, recurring=None)
        await getattr(controller, name)(SimpleNamespace(effective_message=message), None)
        return "".join(message.chunks)

    for name in ("start", "help"):
        assert js["staticCommands"][name] == asyncio.run(static_reply(name))
    async def stock_reply(case: dict) -> str:
        class Message:
            text = "/stock" + (" " + case["raw"] if case["raw"] else "")

            def __init__(self) -> None:
                self.chunks: list[str] = []

            async def reply_text(self, value: str, **_kwargs: object) -> None:
                self.chunks.append(value)

        class PriceProvider:
            async def get_price(self, market: str, ticker: str) -> object:
                del market, ticker
                quote = case["quote"]
                if quote is None:
                    raise RuntimeError("synthetic unavailable")
                return SimpleNamespace(
                    price=Decimal(quote["price"]), currency=Currency(quote["currency"]),
                    as_of=datetime.fromisoformat(quote["asOf"].replace("Z", "+00:00")),
                    stale=quote["stale"], provider=quote["provider"],
                )

        message = Message()
        controller = TelegramCommandController(
            settings=None, portfolios=None, recurring=None,
            analysis=(SimpleNamespace(provider=PriceProvider())
                      if case["providerConfigured"] else None),
        )
        await controller.stock(SimpleNamespace(effective_message=message), None)
        return "".join(message.chunks)

    for case, actual in zip(fixture["stockCases"], js["stockReplies"], strict=True):
        assert actual == asyncio.run(stock_reply(case))
    assert js["money"] == [_money(Decimal(value), places) for value, places in fixture["money"]]
    for (left, right), actual in zip(fixture["divisions"], js["divisions"], strict=True):
        assert Decimal(actual) == Decimal(left) / Decimal(right)

    for pair, actual in zip(fixture["budgets"], js["budgets"], strict=True):
        plans, fx = pair
        if fx is None and any(p["enabled"] and p["currency"] == "USD" for p in plans):
            assert actual is None
            continue
        expected = sum((
            Decimal(p["dailyAmount"]) * len(set(p["scheduledWeekdays"])) *
            (Decimal(fx) if p["currency"] == "USD" else 1)
            for p in plans if p["enabled"]
        ), Decimal("0"))
        assert Decimal(actual.replace(",", "")) == expected

    for args, actual in zip(fixture["schedules"], js["schedules"], strict=True):
        stamp, zone, daily, weekly, weekday = args
        assert actual == list(due_jobs(
            datetime.fromisoformat(stamp.replace("Z", "+00:00")),
            timezone_name=zone, daily_time=time.fromisoformat(daily),
            weekly_time=time.fromisoformat(weekly), weekly_weekday=weekday,
        ))
        assert ZoneInfo(zone) is not None

    for case, actual in zip(fixture["admissions"], js["admissions"], strict=True):
        assert actual == admitted_update_id(
            dict(case["headers"]), json.dumps(case["update"]).encode(),
            expected_secret=case["secret"], allowed_user_ids=set(case["allowed"]),
        )

    for case, actual in zip(fixture["ledgers"], js["ledgers"], strict=True):
        plan_data = case["plan"]
        plan = RecurringBuyPlan(
            InstrumentKey("US", "TEST", asset_type="ETF", currency=Currency.KRW),
            Money(plan_data["dailyAmount"], Currency.KRW), enabled=plan_data["enabled"],
            broker=plan_data["broker"], account_id=plan_data["accountId"],
            scheduled_weekdays=tuple(plan_data["scheduledWeekdays"]),
        )
        transactions = [Transaction(
            row["transactionId"], row["broker"], row["accountId"],
            InstrumentKey(row["instrument"]["market"], "TEST", asset_type="ETF",
                          currency=Currency.KRW),
            row["side"], Decimal("1"), Decimal("1"), date.fromisoformat(row["tradeDate"]),
            gross_amount=Decimal(row["grossAmount"]),
        ) for row in case["transactions"]]
        opts = case["options"]
        evaluated = evaluate_recurring_buy(
            plan, transactions, target_date=date.fromisoformat(opts["targetDate"]),
            now=datetime.fromisoformat("2026-10-26T20:00:00+00:00"),
            cutoff_at=datetime.fromisoformat(
                "2026-10-26T19:00:00+00:00" if opts["cutoffPassed"]
                else "2026-10-26T21:00:00+00:00"
            ),
            data_complete=opts["dataComplete"], is_trading_day=opts["isTradingDay"],
            tolerance_pct=Decimal(opts["tolerancePct"]),
            absolute_tolerance=Decimal(opts["absoluteTolerance"]),
        )
        assert actual == {
            "status": evaluated.status.value,
            "actual": _money(evaluated.actual.amount, 4),
            "remaining": _money(evaluated.remaining.amount, 4),
            "matchedTransactionIds": list(evaluated.matched_transaction_ids),
            "isFinal": evaluated.is_final,
        }

    for index, (case, actual) in enumerate(zip(
        fixture["aggregations"], js["aggregations"], strict=True
    )):
        def result(row: dict, *, cash: bool) -> BrokerResult:
            items = tuple(
                CashBalance(item["broker"], item["accountId"], item["currency"],
                            Decimal(item["amount"]))
                if cash else Position(
                    item["broker"], item["accountId"],
                    InstrumentKey(item["instrument"]["market"],
                                  item["instrument"]["ticker"], "ETF",
                                  item["instrument"]["currency"]),
                    Decimal(item["quantity"]),
                    cost_basis=(None if item["costBasis"] is None
                                else Decimal(item["costBasis"])),
                    market_value=(None if item["marketValue"] is None
                                  else Decimal(item["marketValue"])),
                ) for item in row["items"]
            )
            errors = (BrokerError("synthetic_partial", "fixture"),) if row["status"] == "partial" else ()
            return BrokerResult(row["broker"], BrokerStatus(row["status"]), items, errors)

        positions = tuple(result(row, cash=False) for row in case["positionResults"])
        cash = tuple(result(row, cash=True) for row in case.get("cashResults", []))
        quotes = tuple(FxQuote(row["base"], row["quote"], Decimal(row["rate"]),
                               is_stale=row["isStale"])
                       for row in case.get("fxQuotes", []))
        snapshot = PortfolioAggregator(Currency(case["baseCurrency"]), quotes).aggregate(
            positions, expected_brokers=case["expectedBrokers"], cash_results=cash,
        )
        refresh = PortfolioRefresh(
            snapshot=snapshot, account_results=(), position_results=positions,
            cash_results=cash, fx_quote=quotes[0] if quotes else None,
            collected_at=datetime.fromisoformat("2026-02-02T01:00:00+00:00"),
        )
        expected_assets = format_assets(refresh)
        expected_portfolio = format_portfolio(snapshot)
        if {broker.strip().lower() for broker in case["expectedBrokers"]} != {"kiwoom_main"}:
            expected_assets = expected_assets.replace("키움 메인", "선택 출처", 1)
            expected_portfolio = expected_portfolio.replace("키움 메인", "선택 출처", 1)
        assert js["assetReports"][index] == expected_assets
        assert js["portfolioReports"][index] == expected_portfolio
        expected_periodic = {
            "daily": build_daily_report(refresh),
            "weekly": build_weekly_report(refresh),
        }
        if {broker.strip().lower() for broker in case["expectedBrokers"]} != {"kiwoom_main"}:
            expected_periodic = {
                key: value.replace("키움 메인 확인 자산 부분 합계", "선택 출처 확인 자산 부분 합계", 1)
                .replace("키움 메인 전체자산", "선택 출처 전체자산", 1)
                .replace("키움 메인 포트폴리오", "선택 출처 포트폴리오", 1)
                for key, value in expected_periodic.items()}
        assert js["periodicReports"][index] == expected_periodic
        expected = {
            "expectedBrokers": list(snapshot.expected_brokers),
            "includedBrokers": list(snapshot.included_brokers),
            "incompleteBrokers": list(snapshot.incomplete_brokers),
            "includesCash": snapshot.includes_cash,
            "isComplete": snapshot.is_complete,
            "includedPositionValue": snapshot.included_position_value.amount,
            "includedCashValue": snapshot.included_cash_value.amount,
            "includedMarketValue": snapshot.included_market_value.amount,
            "totalMarketValue": (None if snapshot.total_market_value is None
                                 else snapshot.total_market_value.amount),
            "includedCostBasis": (None if snapshot.included_cost_basis is None
                                  else snapshot.included_cost_basis.amount),
            "totalCostBasis": (None if snapshot.total_cost_basis is None
                               else snapshot.total_cost_basis.amount),
            "cashWeightPct": snapshot.cash_weight_pct,
            "warnings": list(snapshot.warnings),
        }
        for key, value in expected.items():
            if isinstance(value, Decimal):
                assert Decimal(actual[key]) == value, key
            else:
                assert actual[key] == value, key
        assert len(actual["positions"]) == len(snapshot.positions)
        for js_position, py_position in zip(actual["positions"], snapshot.positions, strict=True):
            assert js_position["instrument"] == {
                "market": py_position.instrument.market,
                "ticker": py_position.instrument.ticker,
                "assetType": py_position.instrument.asset_type.value,
                "currency": py_position.instrument.currency.value,
            }
            for js_name, py_value in {
                "quantity": py_position.quantity,
                "averagePrice": py_position.average_price,
                "currentPrice": py_position.current_price,
                "costBasis": py_position.cost_basis,
                "marketValue": py_position.market_value,
                "unrealizedProfit": py_position.unrealized_profit,
                "returnPct": py_position.return_pct,
                "costBasisBase": py_position.cost_basis_base,
                "marketValueBase": py_position.market_value_base,
                "weightPct": py_position.weight_pct,
            }.items():
                assert (None if js_position[js_name] is None else Decimal(js_position[js_name])) == py_value
            assert [(broker, Decimal(value)) for broker, value in js_position["brokerQuantities"]] == [
                (item.broker, item.quantity) for item in py_position.broker_quantities
            ]
        assert len(actual["cashBalances"]) == len(snapshot.cash_balances)
        for js_cash, py_cash in zip(actual["cashBalances"], snapshot.cash_balances, strict=True):
            assert (js_cash["broker"], js_cash["accountId"], js_cash["currency"]) == (
                py_cash.broker, py_cash.account_id, py_cash.currency.value)
            assert Decimal(js_cash["amount"]) == py_cash.amount
            assert (None if js_cash["amountBase"] is None
                    else Decimal(js_cash["amountBase"])) == py_cash.amount_base

    for case, actual in zip(fixture["accountReports"], js["accountReports"], strict=True):
        results = tuple(BrokerResult(
            row["broker"], row["status"], tuple(Account(
                row["broker"], item["accountId"], item["displayName"]
            ) for item in row["items"])
        ) for row in case["results"])
        snapshot = PortfolioAggregator(Currency.KRW).aggregate(
            (BrokerResult.ok("kiwoom_main"),) if case["isComplete"] else (),
            expected_brokers=("kiwoom_main",),
        )
        refresh = PortfolioRefresh(
            snapshot=snapshot, account_results=results, position_results=(),
            cash_results=(), fx_quote=None,
            collected_at=datetime.fromisoformat("2026-02-02T01:00:00+00:00"),
        )
        assert actual == format_accounts(refresh)


def test_js_new_money_rebalance_matches_python_on_synthetic_positions() -> None:
    instrument_a = {"market": "US", "ticker": "ALPHA", "assetType": "ETF", "currency": "USD"}
    instrument_b = {"market": "US", "ticker": "BETA", "assetType": "ETF", "currency": "USD"}
    cases = [
        {"positionResults": [_result("kiwoom_main", items=[
            _position("kiwoom_main", ticker="ALPHA", market_value="100"),
            _position("kiwoom_main", ticker="BETA", market_value="50")])],
         "expectedBrokers": ["kiwoom_main"], "baseCurrency": "USD", "newMoney": "30",
         "targets": [{"instrument": instrument_a, "targetWeightPct": "40"},
                     {"instrument": instrument_b, "targetWeightPct": "60"}]},
        {"positionResults": [_result("kiwoom_main", "partial", items=[
            _position("kiwoom_main", ticker="ALPHA", market_value="100")])],
         "expectedBrokers": ["kiwoom_main"], "baseCurrency": "USD", "newMoney": "30",
         "targets": [{"instrument": instrument_a, "targetWeightPct": "100"}]},
    ]
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "rebalanceCases": [
                   {"aggregation": {key: value for key, value in case.items()
                                    if key not in ("newMoney", "targets")},
                    "newMoney": case["newMoney"], "targets": case["targets"]}
                   for case in cases]}
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True, check=True)
    for case, actual in zip(cases, json.loads(result.stdout)["rebalanceCases"], strict=True):
        rows = case["positionResults"][0]
        positions = tuple(Position("kiwoom_main", item["accountId"],
                                   InstrumentKey(item["instrument"]["market"],
                                                 item["instrument"]["ticker"], "ETF", "USD"),
                                   Decimal(item["quantity"]),
                                   cost_basis=Decimal(item["costBasis"]),
                                   market_value=Decimal(item["marketValue"]))
                          for item in rows["items"])
        snapshot = PortfolioAggregator(Currency.USD).aggregate(
            (BrokerResult("kiwoom_main", rows["status"], positions,
                          (BrokerError("synthetic_partial", "fixture"),)
                          if rows["status"] == "partial" else ()),),
            expected_brokers=("kiwoom_main",))
        targets = tuple(TargetAllocation(InstrumentKey(item["instrument"]["market"],
                                                       item["instrument"]["ticker"], "ETF", "USD"),
                                         Decimal(item["targetWeightPct"]))
                        for item in case["targets"])
        expected = suggest_new_money_rebalance(snapshot, targets,
                                               Money(case["newMoney"], Currency.USD))
        assert actual["report"] == format_rebalance(expected)
        plan = actual["plan"]
        assert plan["isEligible"] == expected.is_eligible
        assert plan["usesSales"] is False
        assert Decimal(plan["allocatedNewMoney"]) == expected.allocated_new_money.amount
        assert Decimal(plan["unallocatedCash"]) == expected.unallocated_cash.amount
        assert list(plan["warnings"]) == list(expected.warnings)
        assert len(plan["items"]) == len(expected.items)
        for js, py in zip(plan["items"], expected.items, strict=True):
            assert js["instrument"]["ticker"] == py.instrument.ticker
            assert Decimal(js["suggestedNewMoney"]) == py.suggested_new_money.amount
            assert js["action"] == py.action.value


def test_js_no_analysis_recurring_report_matches_python_synthetic_plans() -> None:
    plans_data = [
        {"instrument": {"market": "NASDAQ", "ticker": "QQQ", "assetType": "ETF",
                        "currency": "USD"}, "dailyAmount": "10.5", "currency": "USD",
         "enabled": True, "broker": "kiwoom_accum", "scheduledWeekdays": [0],
         "investmentThesis": "synthetic <idea>"},
        {"instrument": {"market": "NYSE", "ticker": "VOO", "assetType": "ETF",
                        "currency": "USD"}, "dailyAmount": "20", "currency": "USD",
         "enabled": True, "broker": "meritz", "scheduledWeekdays": [0, 1, 2, 3, 4],
         "investmentThesis": ""},
        {"instrument": {"market": "NASDAQ", "ticker": "AMD", "assetType": "EQUITY",
                        "currency": "USD"}, "dailyAmount": "700", "currency": "KRW",
         "enabled": True, "broker": "meritz", "scheduledWeekdays": [4],
         "investmentThesis": ""},
    ]
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "recurringCases": [{"plans": plans_data}]}
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True, check=True)
    plans = tuple(RecurringBuyPlan(
        InstrumentKey(item["instrument"]["market"], item["instrument"]["ticker"],
                      item["instrument"]["assetType"], item["instrument"]["currency"]),
        Money(item["dailyAmount"], item["currency"]), enabled=item["enabled"],
        broker=item["broker"], scheduled_weekdays=tuple(item["scheduledWeekdays"]),
        investment_thesis=item["investmentThesis"])
        for item in plans_data)
    snapshot = PortfolioAggregator(Currency.KRW).aggregate(
        (BrokerResult.ok("kiwoom_main"),), expected_brokers=("kiwoom_main",))
    assert json.loads(result.stdout)["recurringReports"][0] == format_recurring(plans, snapshot)


def test_js_nonempty_periodic_reports_match_python_without_analysis() -> None:
    plan_data = {"instrument": {"market": "KOSPI", "ticker": "005930",
                                "assetType": "EQUITY", "currency": "KRW"},
                 "dailyAmount": "1000", "currency": "KRW", "enabled": True,
                 "broker": "meritz", "scheduledWeekdays": [0],
                 "targetWeightPct": None, "investmentThesis": ""}
    aggregation = {"baseCurrency": "KRW", "expectedBrokers": ["kiwoom_main"],
                   "positionResults": [_result("kiwoom_main")], "cashResults": []}
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "periodicPlanCases": [
                   {"aggregation": aggregation, "plans": [plan_data]}]}
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True, check=True)
    plan = RecurringBuyPlan(
        InstrumentKey("KOSPI", "005930", "EQUITY", "KRW"),
        Money("1000", Currency.KRW), broker="meritz", scheduled_weekdays=(0,))
    snapshot = PortfolioAggregator(Currency.KRW).aggregate(
        (BrokerResult.ok("kiwoom_main"),), expected_brokers=("kiwoom_main",))
    refresh = PortfolioRefresh(snapshot=snapshot, account_results=(), position_results=(
        BrokerResult.ok("kiwoom_main"),), cash_results=(), fx_quote=None,
        collected_at=datetime.fromisoformat("2026-02-02T01:00:00+00:00"))
    assert json.loads(result.stdout)["periodicPlanReports"][0] == {
        "daily": build_daily_report(refresh, recurring_plans=(plan,)),
        "weekly": build_weekly_report(refresh, recurring_plans=(plan,)),
    }


def test_js_recurring_analysis_recommendation_matches_python() -> None:
    plan_data = {"instrument": {"market": "NASDAQ", "ticker": "TEST",
                                "assetType": "ETF", "currency": "USD"},
                 "dailyAmount": "20", "currency": "USD", "enabled": True,
                 "broker": "kiwoom_accum", "scheduledWeekdays": [0],
                 "investmentThesis": "fixture"}
    analysis = {"companyScore": {"score": "70", "components": [
        {"name": "fundamental", "score": "60"},
        {"name": "valuation", "score": "75"},
        {"name": "growth", "score": "60"},
        {"name": "moat", "score": None},
        {"name": "safety", "score": "50"}]},
        "recurringSuitability": {"score": "61", "reasons": ["fixture trend"]},
        "finalScore": {"hardWarnings": [], "decision": "continue_buy"},
        "fundamentals": {"sector": "fixture sector"}, "valuation": None,
        "earnings": None, "news": []}
    aggregation = {"baseCurrency": "KRW", "expectedBrokers": ["kiwoom_main"],
                   "positionResults": [_result("kiwoom_main")],
                   "fxQuotes": [{"base": "USD", "quote": "KRW", "rate": "1400",
                                 "isStale": False}]}
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "recurringCases": [{"plans": [plan_data],
                 "analyses": {"NASDAQ:TEST:ETF:USD": analysis}}],
               "periodicPlanCases": [{"aggregation": aggregation, "plans": [plan_data],
                                      "analyses": {"NASDAQ:TEST:ETF:USD": analysis}}]}
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True, check=True)
    plan = RecurringBuyPlan(InstrumentKey("NASDAQ", "TEST", "ETF", "USD"),
                            Money("20", Currency.USD), broker="kiwoom_accum",
                            scheduled_weekdays=(0,), investment_thesis="fixture")
    py_analysis = SimpleNamespace(
        company_score=SimpleNamespace(score=Decimal("70"), components=(
            SimpleNamespace(name="fundamental", score=Decimal("60")),
            SimpleNamespace(name="valuation", score=Decimal("75")),
            SimpleNamespace(name="growth", score=Decimal("60")),
            SimpleNamespace(name="moat", score=None),
            SimpleNamespace(name="safety", score=Decimal("50")))),
        recurring_suitability=SimpleNamespace(score=Decimal("61"),
                                              reasons=("fixture trend",)),
        final_score=SimpleNamespace(hard_warnings=(),
                                    decision=SimpleNamespace(value="continue_buy")),
        fundamentals=SimpleNamespace(sector="fixture sector"),
        valuation=None, earnings=None, news=())
    snapshot = PortfolioAggregator(Currency.USD).aggregate(
        (BrokerResult.ok("kiwoom_main"),), expected_brokers=("kiwoom_main",))
    assert json.loads(result.stdout)["recurringReports"][0] == format_recurring(
        (plan,), snapshot, {plan.instrument.stable_id: py_analysis})
    quote = FxQuote("USD", "KRW", Decimal("1400"))
    position_results = (BrokerResult.ok("kiwoom_main"),)
    report_snapshot = PortfolioAggregator(Currency.KRW, (quote,)).aggregate(
        position_results, expected_brokers=("kiwoom_main",))
    refresh = PortfolioRefresh(snapshot=report_snapshot, account_results=(),
                               position_results=position_results, cash_results=(),
                               fx_quote=quote,
                               collected_at=datetime.fromisoformat("2026-02-02T01:00:00+00:00"))
    assert json.loads(result.stdout)["periodicPlanReports"][0] == {
        "daily": build_daily_report(refresh, recurring_plans=(plan,),
                                    analyses={plan.instrument.stable_id: py_analysis}),
        "weekly": build_weekly_report(refresh, recurring_plans=(plan,),
                                      analyses={plan.instrument.stable_id: py_analysis}),
    }


def test_js_recurring_weekday_and_currency_budget_matches_python() -> None:
    cases = [("MON", "2000", "KRW", [0]),
             ("FIVE", "1000", "KRW", [0, 1, 2, 3, 4]),
             ("DOLLAR", "10", "USD", [4])]
    js_plans = [{"instrument": {"market": "NASDAQ", "ticker": ticker,
                  "assetType": "ETF", "currency": "USD"},
                 "dailyAmount": amount, "currency": currency, "enabled": True,
                 "broker": "kiwoom_accum", "scheduledWeekdays": days}
                for ticker, amount, currency, days in cases]
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "recurringCases": [{"plans": js_plans, "analyses": {}}],
               "periodicPlanCases": []}
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                            check=True)
    plans = tuple(RecurringBuyPlan(
        InstrumentKey("NASDAQ", ticker, "ETF", "USD"),
        Money(amount, Currency(currency)), broker="kiwoom_accum",
        scheduled_weekdays=tuple(days)) for ticker, amount, currency, days in cases)
    snapshot = PortfolioAggregator(Currency.KRW).aggregate(())
    rendered = json.loads(result.stdout)["recurringReports"][0]
    assert rendered == format_recurring(plans, snapshot)
    assert "주간 예정 투입 7,000원" in rendered
    assert "주간 예정 투입 $10.00" in rendered
    assert "노출군 분류 확인 불가" in rendered

    no_day = {**js_plans[0], "scheduledWeekdays": []}
    fixture["recurringCases"] = [{"plans": [no_day], "analyses": {}}]
    result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                            check=True)
    unscheduled = RecurringBuyPlan(
        InstrumentKey("NASDAQ", "MON", "ETF", "USD"),
        Money("2000", Currency.KRW), broker="kiwoom_accum", scheduled_weekdays=())
    rendered = json.loads(result.stdout)["recurringReports"][0]
    assert rendered == format_recurring((unscheduled,), snapshot)
    assert "예정 요일 없음" in rendered
    assert "노출군별 예정 투입 비중 (같은 통화의 주간 합계 기준)\n• 분류 확인 불가 확인 불가" in rendered


def test_js_recurring_warning_and_identifier_scope_match_python() -> None:
    def compare(plans_data: list[dict]) -> str:
        fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
                   "admissions": [], "ledgers": [], "aggregations": [],
                   "accountReports": [], "stockCases": [],
                   "recurringCases": [{"plans": plans_data, "analyses": {}}],
                   "periodicPlanCases": []}
        result = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                                input=json.dumps(fixture), encoding="utf-8",
                                capture_output=True, check=True)
        plans = tuple(RecurringBuyPlan(
            InstrumentKey(data["instrument"]["market"],
                          data["instrument"]["ticker"],
                          data["instrument"]["assetType"], "USD"),
            Money(data["dailyAmount"], Currency.USD),
            scheduled_weekdays=tuple(data["scheduledWeekdays"]))
            for data in plans_data)
        rendered = json.loads(result.stdout)["recurringReports"][0]
        assert rendered == format_recurring(plans,
                                            PortfolioAggregator(Currency.USD).aggregate(()))
        return rendered

    def plan(market: str, ticker: str, amount: str, days: list[int],
             asset_type: str = "ETF") -> dict:
        return {"instrument": {"market": market, "ticker": ticker,
                               "assetType": asset_type, "currency": "USD"},
                "dailyAmount": amount, "currency": "USD", "enabled": True,
                "scheduledWeekdays": days}

    near_threshold = compare([plan("US", "QQQ", "499", [0]),
                              plan("US", "VOO", "1501", [0])])
    assert "• 미국 대형 기술·성장 ETF 25.0%\n" in near_threshold
    assert "• 미국 대형 기술·성장 ETF 25.0% ⚠️" not in near_threshold
    duplicate_days = compare([plan("US", "QQQ", "10", [0, 0])])
    assert "주간 예정 투입 $10.00" in duplicate_days
    other_listing = compare([plan("KOSPI", "QQQ", "10", [0])])
    assert "노출군 분류 확인 불가" in other_listing
    other_type = compare([plan("US", "QQQ", "10", [0], "EQUITY")])
    assert "노출군 분류 확인 불가" in other_type
