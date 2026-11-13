"""Synthetic Python/JS parity for the analysis HTML boundary."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

from account_bot.domain import BrokerResult, Currency, InstrumentKey, Position
from account_bot.market import (
    EarningsData,
    FundamentalData,
    NewsItem,
    PriceQuote,
    TrendData,
    ValuationData,
)
from account_bot.portfolio import PortfolioAggregator
from account_bot.reports.formatter import format_analysis
from account_bot.services.analysis_service import CompanyAnalysisService

ROOT = Path(__file__).resolve().parents[1]


def test_analysis_report_preserves_missing_and_verified_values() -> None:
    fields = {"market": "NASDAQ", "ticker": "TEST", "price": None,
              "fundamentals": None, "valuation": None, "earnings": None,
              "news": [], "reasons": ["판단 데이터 부족"], "warnings": ["price:Unavailable"],
              "companyScore": {"score": None, "components": [
                  {"score": None} for _ in range(5)]},
              "portfolioScore": {"score": None}, "finalScore": {
                  "score": None, "decision": "unavailable"},
              "recurringSuitability": {"score": None}}
    rich = json.loads(json.dumps(fields))
    rich.update({"price": {"price": "20.125", "currency": "USD", "stale": True},
                 "fundamentals": {"revenueGrowth": "0.12", "operatingMargin": "0.20",
                                  "freeCashFlow": "12345.5"},
                 "valuation": {"closePrice": "20.125", "asOf": "2026-02-02T01:00:00Z",
                               "forwardPe": "15.125", "historicalForwardPeMedian": None,
                               "peg": "1.2", "enterpriseToEbitda": "11.5",
                               "freeCashFlowYield": "0.0475", "dilutionPct": "-1.25"},
                 "earnings": {"period": "Q1 <test>", "revenue": "12000.5",
                              "revenueYoy": "0.15", "eps": "1.235", "epsYoy": "-0.03",
                              "grossMargin": "0.40", "operatingMargin": "0.21",
                              "guidance": "fixture <guidance>"},
                 "news": [{"fundamentalImpact": "REVIEW_REQUIRED",
                           "title": "Fixture <review>", "url": "https://example.invalid/news"}],
                 "companyScore": {"score": "70", "components": [
                     {"score": "70"}, {"score": "80"}, {"score": "60"},
                     {"score": None}, {"score": "50"}]},
                 "portfolioScore": {"score": "55"},
                 "finalScore": {"score": "80", "decision": "continue_buy"},
                 "recurringSuitability": {"score": "61"}})
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "analysisCases": [fields, rich]}
    output = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                            check=True).stdout

    def py(case: dict) -> NS:
        def dec(value: str | None) -> Decimal | None:
            return None if value is None else Decimal(value)

        def nested(data: dict | None, names: dict[str, str]) -> NS | None:
            return None if data is None else NS(**{
                py_name: dec(data.get(js_name)) if js_name != "asOf" else datetime(
                    2026, 2, 2, 1, tzinfo=timezone.utc)
                for js_name, py_name in names.items()})

        f = nested(case["fundamentals"], {"revenueGrowth": "revenue_growth",
                                          "operatingMargin": "operating_margin",
                                          "freeCashFlow": "free_cash_flow"})
        v = nested(case["valuation"], {"closePrice": "close_price", "asOf": "as_of",
                                       "forwardPe": "forward_pe",
                                       "historicalForwardPeMedian": "historical_forward_pe_median",
                                       "peg": "peg", "enterpriseToEbitda": "enterprise_to_ebitda",
                                       "freeCashFlowYield": "free_cash_flow_yield",
                                       "dilutionPct": "dilution_pct"})
        e = nested(case["earnings"], {"revenue": "revenue", "revenueYoy": "revenue_yoy",
                                      "eps": "eps", "epsYoy": "eps_yoy",
                                      "grossMargin": "gross_margin",
                                      "operatingMargin": "operating_margin"})
        if e is not None:
            e.period = case["earnings"]["period"]
            e.guidance = case["earnings"]["guidance"]
        return NS(market=case["market"], ticker=case["ticker"],
                  price=None if case["price"] is None else NS(
                      price=dec(case["price"]["price"]), currency=case["price"]["currency"],
                      stale=case["price"]["stale"]),
                  fundamentals=f, valuation=v, earnings=e,
                  company_score=NS(score=dec(case["companyScore"]["score"]),
                                   components=[NS(score=dec(item["score"]))
                                               for item in case["companyScore"]["components"]]),
                  portfolio_score=NS(score=dec(case["portfolioScore"]["score"])),
                  final_score=NS(score=dec(case["finalScore"]["score"]),
                                 decision=NS(value=case["finalScore"]["decision"])),
                  recurring_suitability=NS(score=dec(case["recurringSuitability"]["score"])),
                  news=[NS(fundamental_impact=item["fundamentalImpact"],
                           title=item["title"], url=item["url"]) for item in case["news"]],
                  reasons=tuple(case["reasons"]), warnings=tuple(case["warnings"]))

    assert json.loads(output)["analysisReports"] == [format_analysis(py(case))
                                                      for case in (fields, rich)]


def test_no_data_analysis_withholds_scores_like_python() -> None:
    class EmptyProvider:
        async def get_price(self, *_args: object) -> None:
            return None

        async def get_fundamentals(self, *_args: object) -> None:
            return None

        async def get_valuation(self, *_args: object) -> None:
            return None

        async def get_earnings(self, *_args: object) -> None:
            return None

        async def get_news(self, *_args: object, **_kwargs: object) -> list:
            return []

    snapshot = PortfolioAggregator(Currency.KRW).aggregate(
        (BrokerResult.ok("kiwoom_main"),), expected_brokers=("kiwoom_main",))
    py_result = asyncio.run(CompanyAnalysisService(EmptyProvider()).analyze(
        market="NASDAQ", ticker="TEST", portfolio=snapshot, target_weight_pct=None))
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "analysisComputations": [{
                   "market": "NASDAQ", "ticker": "TEST", "targetWeightPct": None,
                   "aggregation": {"baseCurrency": "KRW", "expectedBrokers": ["kiwoom_main"],
                                   "positionResults": [{"broker": "kiwoom_main",
                                                       "status": "ok", "items": []}]},
                   "errors": {"trend": "NotSupported"}}]}
    output = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                            check=True).stdout
    actual = json.loads(output)["analysisComputations"][0]
    assert actual["report"] == format_analysis(py_result)
    assert actual["result"]["finalScore"]["decision"] == py_result.final_score.decision.value
    assert actual["result"]["warnings"] == list(py_result.warnings)
    assert actual["result"]["reasons"] == list(py_result.reasons)


def test_normalized_analysis_matches_python_with_synthetic_provider_data() -> None:
    stamp = datetime(2026, 2, 2, 1, tzinfo=timezone.utc)
    instrument = InstrumentKey("NASDAQ", "TEST", "ETF", "USD")
    result = BrokerResult.ok("kiwoom_main", (Position(
        "kiwoom_main", "fixture-account", instrument, Decimal("1"),
        cost_basis=Decimal("80"), market_value=Decimal("100")),))
    snapshot = PortfolioAggregator(Currency.USD).aggregate(
        (result,), expected_brokers=("kiwoom_main",))

    class SyntheticProvider:
        async def get_price(self, *_args: object) -> PriceQuote:
            return PriceQuote("NASDAQ", "TEST", "USD", Decimal("100"), stamp, "fixture")

        async def get_fundamentals(self, *_args: object) -> FundamentalData:
            return FundamentalData("NASDAQ", "TEST", stamp, "fixture",
                                   revenue_growth=Decimal("0.12"),
                                   earnings_growth=Decimal("0.20"),
                                   gross_margin=Decimal("0.4"),
                                   operating_margin=Decimal("0.2"),
                                   free_cash_flow=Decimal("100"),
                                   return_on_equity=Decimal("0.18"),
                                   total_cash=Decimal("200"), total_debt=Decimal("300"),
                                   debt_to_equity=Decimal("75"))

        async def get_valuation(self, *_args: object) -> ValuationData:
            return ValuationData("NASDAQ", "TEST", stamp, "fixture",
                                 forward_pe=Decimal("24"), peg=Decimal("1.6"),
                                 enterprise_to_ebitda=Decimal("18"),
                                 free_cash_flow_yield=Decimal("0.04"),
                                 historical_forward_pe_median=Decimal("22"),
                                 dilution_pct=Decimal("2"))

        async def get_earnings(self, *_args: object) -> EarningsData:
            return EarningsData("NASDAQ", "TEST", "Q1", stamp, stamp, "fixture",
                                revenue=Decimal("1000"), revenue_yoy=Decimal("0.1"))

        async def get_news(self, *_args: object, **_kwargs: object) -> list[NewsItem]:
            return [NewsItem("TEST", "Fixture earnings review",
                             "https://example.invalid/fixture", stamp, "fixture")]

        async def get_trend(self, *_args: object) -> TrendData:
            return TrendData("NASDAQ", "TEST", stamp, "fixture", 600,
                             return_1y=Decimal("0.15"), cagr_3y=Decimal("0.1"),
                             max_drawdown_3y=Decimal("-0.3"),
                             annualized_volatility_1y=Decimal("0.25"),
                             above_200d_average=True)

    py_result = asyncio.run(CompanyAnalysisService(SyntheticProvider()).analyze(
        market="NASDAQ", ticker="TEST", portfolio=snapshot,
        target_weight_pct=Decimal("50")))
    input_data = {"market": "NASDAQ", "ticker": "TEST", "targetWeightPct": "50",
                  "aggregation": {"baseCurrency": "USD", "expectedBrokers": ["kiwoom_main"],
                                  "positionResults": [{"broker": "kiwoom_main", "status": "ok",
                                                       "items": [{"broker": "kiwoom_main",
                                                                  "accountId": "fixture-account",
                                                                  "instrument": {"market": "NASDAQ",
                                                                                 "ticker": "TEST",
                                                                                 "assetType": "ETF",
                                                                                 "currency": "USD"},
                                                                  "quantity": "1",
                                                                  "costBasis": "80",
                                                                  "marketValue": "100"}]}]},
                  "price": {"market": "NASDAQ", "ticker": "TEST", "currency": "USD",
                            "price": "100", "asOf": "2026-02-02T01:00:00Z",
                            "provider": "fixture", "stale": False},
                  "fundamentals": {"asOf": "2026-02-02T01:00:00Z", "provider": "fixture",
                                   "revenueGrowth": "0.12", "earningsGrowth": "0.20",
                                   "grossMargin": "0.4", "operatingMargin": "0.2",
                                   "freeCashFlow": "100", "returnOnEquity": "0.18",
                                   "totalCash": "200", "totalDebt": "300",
                                   "debtToEquity": "75"},
                  "valuation": {"asOf": "2026-02-02T01:00:00Z", "forwardPe": "24",
                                "peg": "1.6", "enterpriseToEbitda": "18",
                                "freeCashFlowYield": "0.04",
                                "historicalForwardPeMedian": "22", "dilutionPct": "2"},
                  "earnings": {"period": "Q1", "revenue": "1000", "revenueYoy": "0.1"},
                  "news": [{"title": "Fixture earnings review",
                            "url": "https://example.invalid/fixture", "summary": None}],
                  "trend": {"return1y": "0.15", "cagr3y": "0.1",
                            "maxDrawdown3y": "-0.3", "annualizedVolatility1y": "0.25",
                            "above200dAverage": True}}
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "analysisComputations": [input_data]}
    output = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                            input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                            check=True).stdout
    actual = json.loads(output)["analysisComputations"][0]
    assert actual["report"] == format_analysis(py_result)
    assert Decimal(actual["result"]["companyScore"]["score"]) == py_result.company_score.score
    assert Decimal(actual["result"]["portfolioScore"]["score"]) == py_result.portfolio_score.score
    assert Decimal(actual["result"]["finalScore"]["score"]) == py_result.final_score.score
    assert actual["result"]["reasons"] == list(py_result.reasons)
