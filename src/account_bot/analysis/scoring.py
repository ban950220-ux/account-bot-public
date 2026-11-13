"""Auditable score calculations; no LLM or network dependency is permitted here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Iterable, Optional, Tuple

from account_bot.domain import as_decimal

SCORE_QUANTUM = Decimal("0.01")


def _score(value: Optional[Decimal], name: str) -> Optional[Decimal]:
    if value is None:
        return None
    result = as_decimal(value, name=name)
    if result < 0 or result > 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return result


def _weight(value: Decimal, name: str) -> Decimal:
    result = as_decimal(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must not be negative")
    return result


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class WeightedComponent:
    name: str
    score: Optional[Decimal]
    weight: Decimal
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("component name must not be empty")
        object.__setattr__(self, "score", _score(self.score, f"{self.name}.score"))
        object.__setattr__(self, "weight", _weight(self.weight, f"{self.name}.weight"))
        object.__setattr__(self, "reasons", tuple(str(reason) for reason in self.reasons))


@dataclass(frozen=True)
class ScoreResult:
    score: Optional[Decimal]
    coverage_pct: Decimal
    is_eligible: bool
    components: Tuple[WeightedComponent, ...]
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoringWeights:
    fundamental: Decimal = Decimal("35")
    valuation: Decimal = Decimal("25")
    growth: Decimal = Decimal("20")
    moat: Decimal = Decimal("10")
    safety: Decimal = Decimal("10")
    target_alignment: Decimal = Decimal("50")
    single_stock_capacity: Decimal = Decimal("20")
    sector_capacity: Decimal = Decimal("15")
    currency_capacity: Decimal = Decimal("10")
    correlation: Decimal = Decimal("5")
    company_final: Decimal = Decimal("70")
    portfolio_final: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        company_fields = ("fundamental", "valuation", "growth", "moat", "safety")
        portfolio_fields = (
            "target_alignment",
            "single_stock_capacity",
            "sector_capacity",
            "currency_capacity",
            "correlation",
        )
        final_fields = ("company_final", "portfolio_final")
        for name in company_fields + portfolio_fields + final_fields:
            object.__setattr__(self, name, _weight(getattr(self, name), name))
        for names, label in (
            (company_fields, "company"),
            (portfolio_fields, "portfolio"),
            (final_fields, "final"),
        ):
            if sum((getattr(self, name) for name in names), Decimal("0")) != Decimal("100"):
                raise ValueError(f"{label} weights must sum to 100")


@dataclass(frozen=True)
class CompanyScoreInput:
    fundamental: Optional[Decimal]
    valuation: Optional[Decimal]
    growth: Optional[Decimal]
    moat: Optional[Decimal]
    safety: Optional[Decimal]

    def __post_init__(self) -> None:
        for name in ("fundamental", "valuation", "growth", "moat", "safety"):
            object.__setattr__(self, name, _score(getattr(self, name), name))


@dataclass(frozen=True)
class PortfolioScoreInput:
    portfolio_complete: bool
    target_alignment: Optional[Decimal]
    single_stock_capacity: Optional[Decimal]
    sector_capacity: Optional[Decimal]
    currency_capacity: Optional[Decimal]
    correlation: Optional[Decimal]

    def __post_init__(self) -> None:
        for name in (
            "target_alignment",
            "single_stock_capacity",
            "sector_capacity",
            "currency_capacity",
            "correlation",
        ):
            object.__setattr__(self, name, _score(getattr(self, name), name))


class HardWarning(str, Enum):
    ACCOUNTING_ISSUE = "accounting_issue"
    LIQUIDITY_CRISIS = "liquidity_crisis"
    GOING_CONCERN = "going_concern"
    FRAUD = "fraud"
    MAJOR_CUSTOMER_LOSS = "major_customer_loss"
    STRUCTURAL_DETERIORATION = "structural_business_deterioration"
    THESIS_BREAKDOWN = "investment_thesis_breakdown"


class Decision(str, Enum):
    AGGRESSIVE_BUY = "aggressive_buy"
    CONTINUE_BUY = "continue_buy"
    MAINTAIN = "maintain"
    REDUCE = "reduce"
    REVIEW_STOP = "review_stop"
    REVIEW_REQUIRED = "review_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FinalScoreResult:
    score: Optional[Decimal]
    company_score: Optional[Decimal]
    portfolio_score: Optional[Decimal]
    decision: Decision
    is_actionable: bool
    hard_warnings: Tuple[HardWarning, ...] = ()
    warnings: Tuple[str, ...] = ()


def _weighted_result(
    components: Iterable[WeightedComponent],
    *,
    minimum_coverage_pct: Decimal,
    extra_warnings: Iterable[str] = (),
) -> ScoreResult:
    component_tuple = tuple(components)
    minimum_coverage = _score(minimum_coverage_pct, "minimum_coverage_pct")
    assert minimum_coverage is not None
    total_weight = sum((component.weight for component in component_tuple), Decimal("0"))
    if total_weight <= 0:
        raise ValueError("component weights must have a positive sum")
    covered = tuple(component for component in component_tuple if component.score is not None)
    covered_weight = sum((component.weight for component in covered), Decimal("0"))
    coverage = _rounded(covered_weight / total_weight * Decimal("100"))
    warnings = list(extra_warnings)
    if coverage < minimum_coverage:
        warnings.append(f"score_withheld:coverage_{coverage}_below_{_rounded(minimum_coverage)}")
        return ScoreResult(
            score=None,
            coverage_pct=coverage,
            is_eligible=False,
            components=component_tuple,
            warnings=tuple(warnings),
        )
    weighted_sum = sum((component.score * component.weight for component in covered), Decimal("0"))
    result = _rounded(weighted_sum / covered_weight)
    if covered_weight < total_weight:
        warnings.append("score_renormalized_over_available_components")
    return ScoreResult(
        score=result,
        coverage_pct=coverage,
        is_eligible=True,
        components=component_tuple,
        warnings=tuple(warnings),
    )


def calculate_company_score(
    values: CompanyScoreInput,
    *,
    weights: ScoringWeights | None = None,
    minimum_coverage_pct: Decimal = Decimal("70"),
) -> ScoreResult:
    weights = weights or ScoringWeights()
    return _weighted_result(
        (
            WeightedComponent("fundamental", values.fundamental, weights.fundamental),
            WeightedComponent("valuation", values.valuation, weights.valuation),
            WeightedComponent("growth", values.growth, weights.growth),
            WeightedComponent("moat", values.moat, weights.moat),
            # A high value always means safer; raw risk severity must be inverted upstream.
            WeightedComponent("safety", values.safety, weights.safety),
        ),
        minimum_coverage_pct=minimum_coverage_pct,
    )


def calculate_portfolio_score(
    values: PortfolioScoreInput,
    *,
    weights: ScoringWeights | None = None,
    minimum_coverage_pct: Decimal = Decimal("70"),
) -> ScoreResult:
    weights = weights or ScoringWeights()
    components = (
        WeightedComponent("target_alignment", values.target_alignment, weights.target_alignment),
        WeightedComponent(
            "single_stock_capacity",
            values.single_stock_capacity,
            weights.single_stock_capacity,
        ),
        WeightedComponent("sector_capacity", values.sector_capacity, weights.sector_capacity),
        WeightedComponent("currency_capacity", values.currency_capacity, weights.currency_capacity),
        WeightedComponent("correlation", values.correlation, weights.correlation),
    )
    if not values.portfolio_complete:
        # Coverage remains visible for diagnostics, but a partial denominator can
        # never produce an investable portfolio score.
        diagnostic = _weighted_result(
            components,
            minimum_coverage_pct=minimum_coverage_pct,
            extra_warnings=("score_withheld:portfolio_snapshot_incomplete",),
        )
        return ScoreResult(
            score=None,
            coverage_pct=diagnostic.coverage_pct,
            is_eligible=False,
            components=diagnostic.components,
            warnings=diagnostic.warnings,
        )
    return _weighted_result(
        components,
        minimum_coverage_pct=minimum_coverage_pct,
    )


def _decision(score: Decimal) -> Decision:
    if score >= 85:
        return Decision.AGGRESSIVE_BUY
    if score >= 70:
        return Decision.CONTINUE_BUY
    if score >= 55:
        return Decision.MAINTAIN
    if score >= 40:
        return Decision.REDUCE
    return Decision.REVIEW_STOP


def calculate_final_score(
    company: ScoreResult,
    portfolio: ScoreResult,
    *,
    weights: ScoringWeights | None = None,
    hard_warnings: Iterable[HardWarning] = (),
) -> FinalScoreResult:
    weights = weights or ScoringWeights()
    normalized_warnings = tuple(
        warning if isinstance(warning, HardWarning) else HardWarning(str(warning))
        for warning in hard_warnings
    )
    normalized_warnings = tuple(dict.fromkeys(normalized_warnings))
    inherited_warnings = tuple(company.warnings + portfolio.warnings)
    if (
        not company.is_eligible
        or not portfolio.is_eligible
        or company.score is None
        or portfolio.score is None
    ):
        return FinalScoreResult(
            score=None,
            company_score=company.score,
            portfolio_score=portfolio.score,
            decision=(Decision.REVIEW_REQUIRED if normalized_warnings else Decision.UNAVAILABLE),
            is_actionable=False,
            hard_warnings=normalized_warnings,
            warnings=inherited_warnings
            + ("final_score_withheld:input_score_unavailable",)
            + (("hard_warning_override",) if normalized_warnings else ()),
        )
    final_score = _rounded(
        (company.score * weights.company_final + portfolio.score * weights.portfolio_final)
        / (weights.company_final + weights.portfolio_final)
    )
    if normalized_warnings:
        return FinalScoreResult(
            score=final_score,
            company_score=company.score,
            portfolio_score=portfolio.score,
            decision=Decision.REVIEW_REQUIRED,
            is_actionable=False,
            hard_warnings=normalized_warnings,
            warnings=inherited_warnings + ("hard_warning_override",),
        )
    return FinalScoreResult(
        score=final_score,
        company_score=company.score,
        portfolio_score=portfolio.score,
        decision=_decision(final_score),
        is_actionable=True,
        warnings=inherited_warnings,
    )


def calculate_valuation_decision(
    valuation_score: Decimal | None,
    *,
    hard_warnings: Iterable[HardWarning] = (),
) -> FinalScoreResult:
    """Create the actionable conclusion directly from stock-bot valuation metrics."""

    normalized_warnings = tuple(
        dict.fromkeys(
            warning if isinstance(warning, HardWarning) else HardWarning(str(warning))
            for warning in hard_warnings
        )
    )
    score = _score(valuation_score, "valuation_score")
    if score is None:
        return FinalScoreResult(
            score=None,
            company_score=None,
            portfolio_score=None,
            decision=(Decision.REVIEW_REQUIRED if normalized_warnings else Decision.UNAVAILABLE),
            is_actionable=False,
            hard_warnings=normalized_warnings,
            warnings=("valuation_decision_withheld:data_unavailable",),
        )
    rounded = _rounded(score)
    if normalized_warnings:
        return FinalScoreResult(
            score=rounded,
            company_score=rounded,
            portfolio_score=None,
            decision=Decision.REVIEW_REQUIRED,
            is_actionable=False,
            hard_warnings=normalized_warnings,
            warnings=("valuation_only_decision", "hard_warning_override"),
        )
    return FinalScoreResult(
        score=rounded,
        company_score=rounded,
        portfolio_score=None,
        decision=_decision(rounded),
        is_actionable=True,
        warnings=("valuation_only_decision",),
    )


def buy_capacity_score(current_weight_pct: Decimal, target_weight_pct: Decimal) -> Decimal:
    """Score additional-buy capacity: at/below target is 100, 2x target is 0."""

    current = _score(current_weight_pct, "current_weight_pct")
    target = _score(target_weight_pct, "target_weight_pct")
    assert current is not None and target is not None
    if target == 0:
        return Decimal("0")
    if current <= target:
        return Decimal("100")
    overshoot_ratio = (current - target) / target
    return _rounded(max(Decimal("0"), Decimal("100") * (1 - overshoot_ratio)))


def concentration_capacity_score(
    current_exposure_pct: Decimal, maximum_exposure_pct: Decimal
) -> Decimal:
    """Use the same monotonic capacity rule for stock/sector/currency limits."""

    return buy_capacity_score(current_exposure_pct, maximum_exposure_pct)
