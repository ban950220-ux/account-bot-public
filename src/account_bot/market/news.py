"""Conservative news-to-fundamental impact triage.

The classifier does not change an investment score. It only flags articles for
human/LLM review when the title/summary may describe a business-impacting event.
"""

from __future__ import annotations

from dataclasses import replace

from .base import NewsItem

_IMPACT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ACCOUNTING", ("accounting", "restatement", "auditor", "회계", "감사 의견")),
    ("LIQUIDITY", ("bankruptcy", "default", "liquidity crisis", "파산", "채무불이행", "유동성")),
    ("CUSTOMER", ("contract cancelled", "customer loss", "계약 해지", "고객사 이탈")),
    (
        "GUIDANCE",
        ("guidance", "forecast cut", "forecast raised", "가이던스", "전망 하향", "전망 상향"),
    ),
    ("EARNINGS", ("earnings", "revenue", "profit warning", "실적", "매출", "이익 경고")),
    ("REGULATORY", ("investigation", "antitrust", "sanction", "조사", "독점", "제재")),
    (
        "CAPITAL",
        ("share offering", "debt offering", "capital raise", "유상증자", "회사채", "자본 조달"),
    ),
)


def assess_news_impact(item: NewsItem) -> NewsItem:
    haystack = f"{item.title} {item.summary or ''}".casefold()
    hits: list[str] = []
    for category, keywords in _IMPACT_RULES:
        if any(keyword.casefold() in haystack for keyword in keywords):
            hits.append(category)
    if not hits:
        return replace(
            item,
            fundamental_impact="NO_DIRECT_EVIDENCE",
            impact_reasons=("정량 펀더멘털 영향 근거가 제목/요약에서 확인되지 않음",),
        )
    return replace(
        item,
        fundamental_impact="REVIEW_REQUIRED",
        impact_reasons=tuple(
            f"{category} 관련 사건 가능성—원문과 1차 자료 확인 필요" for category in hits[:3]
        ),
    )


def assess_news(items: list[NewsItem]) -> list[NewsItem]:
    return [assess_news_impact(item) for item in items]
