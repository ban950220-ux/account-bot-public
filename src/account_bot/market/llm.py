"""Structured, data-minimizing contract for an optional narrative LLM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

SYSTEM_INSTRUCTION = """당신은 투자 리서치 요약 도우미다.
입력 JSON에 없는 숫자나 사실을 임의로 생성하지 않는다.
계산, 수익률, 비중, valuation 점수는 수정하지 않는다.
뉴스는 검증 가능한 펀더멘털 또는 투자논리 영향이 있을 때만 연결한다.
출력은 요청된 JSON 스키마만 사용하고, 매수·매도 주문을 지시하지 않는다.
불충분한 데이터는 명시적으로 'insufficient_data'로 표시한다.
"""


class NarrativeAnalyzer(Protocol):
    async def analyze(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class DisabledNarrativeAnalyzer:
    reason: str = "LLM provider is not configured"

    async def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "status": "disabled",
            "summary": "정량 데이터 기반 결과만 제공합니다.",
            "reasons": [self.reason],
        }


def build_analysis_payload(
    *,
    ticker: str,
    fundamentals: dict[str, Any],
    valuation: dict[str, Any],
    earnings: dict[str, Any],
    news: list[dict[str, Any]],
    portfolio: dict[str, Any],
    investment_thesis: str,
) -> dict[str, Any]:
    """Return the only information allowed to leave the quantitative layer."""

    return {
        "ticker": ticker,
        "fundamentals": fundamentals,
        "valuation": valuation,
        "earnings": earnings,
        "news": news,
        "portfolio": portfolio,
        "investment_thesis": investment_thesis,
        "constraints": {
            "invent_missing_numbers": False,
            "calculate_scores": False,
            "news_directly_changes_score": False,
        },
    }
