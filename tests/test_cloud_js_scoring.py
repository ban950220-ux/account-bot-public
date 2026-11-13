"""Synthetic parity for the network-free valuation decision core."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from account_bot.analysis.scoring import (
    CompanyScoreInput,
    PortfolioScoreInput,
    buy_capacity_score,
    calculate_company_score,
    calculate_portfolio_score,
    calculate_valuation_decision,
)
from account_bot.market import FundamentalData, TrendData, ValuationData
from account_bot.services.analysis_service import (
    fundamental_component,
    growth_component,
    recurring_suitability,
    safety_component,
    valuation_component,
)

ROOT = Path(__file__).resolve().parents[1]


def test_js_score_and_valuation_decision_match_python() -> None:
    fundamentals = {
        "revenueGrowth": "0.12", "earningsGrowth": "0.21",
        "grossMargin": "0.42", "operatingMargin": "0.16",
        "returnOnEquity": "0.18", "freeCashFlow": "100",
        "totalCash": "200", "totalDebt": "300", "debtToEquity": "75",
    }
    valuation = {"forwardPe": "24", "peg": "1.6", "enterpriseToEbitda": "18",
                 "freeCashFlowYield": "0.04", "historicalForwardPeMedian": "22",
                 "dilutionPct": "2"}
    case = {"fundamentals": fundamentals, "valuation": valuation,
            "company": {"fundamental": "80", "valuation": "60", "growth": "70",
                        "moat": None, "safety": "90"},
            "portfolio": {"complete": False, "target_alignment": "80",
                          "single_stock_capacity": "50", "sector_capacity": None,
                          "currency_capacity": None, "correlation": None},
            "valuationScore": "70.005", "hardWarnings": [],
            "currentWeight": "12", "targetWeight": "10",
            "trend": {"cagr3y": "0.1", "return1y": "0.15",
                      "maxDrawdown3y": "-0.3", "annualizedVolatility1y": "0.25",
                      "above200dAverage": True}}
    fixture = {"money": [], "divisions": [], "budgets": [], "schedules": [],
               "admissions": [], "ledgers": [], "aggregations": [], "accountReports": [],
               "stockCases": [], "scoringCases": [case]}
    completed = subprocess.run(["node", "tests/cloud_js_runner.mjs"], cwd=ROOT,
                               input=json.dumps(fixture), encoding="utf-8", capture_output=True,
                               check=True)
    result = json.loads(completed.stdout)["scoringCases"][0]
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f = FundamentalData("NASDAQ", "TEST", as_of, "synthetic", **{
        "revenue_growth": Decimal(fundamentals["revenueGrowth"]),
        "earnings_growth": Decimal(fundamentals["earningsGrowth"]),
        "gross_margin": Decimal(fundamentals["grossMargin"]),
        "operating_margin": Decimal(fundamentals["operatingMargin"]),
        "return_on_equity": Decimal(fundamentals["returnOnEquity"]),
        "free_cash_flow": Decimal(fundamentals["freeCashFlow"]),
        "total_cash": Decimal(fundamentals["totalCash"]),
        "total_debt": Decimal(fundamentals["totalDebt"]),
        "debt_to_equity": Decimal(fundamentals["debtToEquity"]),
    })
    v = ValuationData("NASDAQ", "TEST", as_of, "synthetic", **{
        "forward_pe": Decimal(valuation["forwardPe"]),
        "peg": Decimal(valuation["peg"]),
        "enterprise_to_ebitda": Decimal(valuation["enterpriseToEbitda"]),
        "free_cash_flow_yield": Decimal(valuation["freeCashFlowYield"]),
        "historical_forward_pe_median": Decimal(valuation["historicalForwardPeMedian"]),
        "dilution_pct": Decimal(valuation["dilutionPct"]),
    })
    for name, expected in {
        "fundamental": fundamental_component(f), "growth": growth_component(f),
        "valuation": valuation_component(v), "safety": safety_component(f),
    }.items():
        assert Decimal(result[name]) == expected
    company = calculate_company_score(CompanyScoreInput(
        Decimal("80"), Decimal("60"), Decimal("70"), None, Decimal("90")))
    portfolio = calculate_portfolio_score(PortfolioScoreInput(
        False, Decimal("80"), Decimal("50"), None, None, None))
    for name, expected in (("company", company), ("portfolio", portfolio)):
        actual = result[name]
        assert actual["score"] is None if expected.score is None else (
            Decimal(actual["score"]) == expected.score)
        assert Decimal(actual["coveragePct"]) == expected.coverage_pct
        assert actual["isEligible"] == expected.is_eligible
        assert actual["warnings"] == list(expected.warnings)
    decision = calculate_valuation_decision(Decimal("70.005"))
    assert Decimal(result["decision"]["score"]) == decision.score
    assert result["decision"]["decision"] == decision.decision.value
    assert result["decision"]["warnings"] == list(decision.warnings)
    assert Decimal(result["capacity"]) == buy_capacity_score(Decimal("12"), Decimal("10"))
    trend = TrendData("NASDAQ", "TEST", as_of, "synthetic", 600,
                      return_1y=Decimal("0.15"), cagr_3y=Decimal("0.1"),
                      max_drawdown_3y=Decimal("-0.3"),
                      annualized_volatility_1y=Decimal("0.25"),
                      above_200d_average=True)
    expected_trend = recurring_suitability(trend)
    assert Decimal(result["suitability"]["score"]) == expected_trend.score
    assert result["suitability"]["reasons"] == list(expected_trend.reasons)
