"""Async SQLAlchemy ORM models for normalized, non-secret portfolio data.

Raw broker/market responses, access tokens, full account identifiers, report
bodies, and Telegram destination IDs deliberately have no columns here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, TypeDecorator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC timestamps and restore tzinfo on SQLite reads."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


MONEY = Numeric(28, 8, asdecimal=True)
RATIO = Numeric(18, 10, asdecimal=True)
SCORE = Numeric(7, 4, asdecimal=True)


class ExactDecimal(TypeDecorator[Decimal]):
    """Round-trip Decimal exactly through SQLite without a binary float."""

    impl = Numeric(38, 18, asdecimal=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(80))
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> Decimal | str | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("binary float values are not accepted")
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("decimal values must be finite")
        return str(decimal_value) if dialect.name == "sqlite" else decimal_value

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


IMPORT_DECIMAL = ExactDecimal()


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Portfolio(TimestampMixin, Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KRW")
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Seoul")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BrokerAccount(TimestampMixin, Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint("broker", "account_fingerprint", name="uq_broker_account_ref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker: Mapped[str] = mapped_column(String(24), nullable=False)
    account_fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    masked_label: Mapped[str | None] = mapped_column(String(32))
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Instrument(TimestampMixin, Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "ticker",
            "asset_type",
            "currency",
            name="uq_instrument_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False, default="EQUITY")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(3))
    sector: Mapped[str | None] = mapped_column(String(80))
    theme: Mapped[str | None] = mapped_column(String(80))


class FxRateObservation(Base):
    __tablename__ = "fx_rate_observations"
    __table_args__ = (
        UniqueConstraint("observation_key", name="uq_fx_observation_key"),
        CheckConstraint("rate > 0", name="ck_fx_rate_positive"),
        Index(
            "ix_fx_pair_effective",
            "base_currency",
            "quote_currency",
            "effective_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(28, 12, asdecimal=True), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_snapshot_idempotency_key"),
        CheckConstraint(
            "status IN ('COLLECTING','COMPLETE','PARTIAL','FAILED')",
            name="ck_snapshot_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    data_as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="COLLECTING")
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_market_value: Mapped[Decimal | None] = mapped_column(MONEY)
    total_cost_basis: Mapped[Decimal | None] = mapped_column(MONEY)
    total_unrealized_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    total_return_pct: Mapped[Decimal | None] = mapped_column(RATIO)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finalized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SnapshotSource(Base):
    __tablename__ = "snapshot_sources"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "source_key", name="uq_snapshot_source"),
        CheckConstraint(
            "status IN ('PENDING','SUCCESS','PARTIAL','FAILED','STALE','UNSUPPORTED')",
            name="ck_snapshot_source_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    safe_error_type: Mapped[str | None] = mapped_column(String(96))
    carried_from_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="SET NULL")
    )


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "account_id", "instrument_id", name="uq_snapshot_position"),
        CheckConstraint("quantity >= 0", name="ck_position_quantity_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(MONEY)
    current_price: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_basis: Mapped[Decimal | None] = mapped_column(MONEY)
    market_value: Mapped[Decimal | None] = mapped_column(MONEY)
    unrealized_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    return_pct: Mapped[Decimal | None] = mapped_column(RATIO)
    price_as_of: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fx_rate_id: Mapped[int | None] = mapped_column(
        ForeignKey("fx_rate_observations.id", ondelete="RESTRICT")
    )
    value_in_base: Mapped[Decimal | None] = mapped_column(MONEY)


class CashSnapshot(Base):
    __tablename__ = "cash_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "account_id", "currency", name="uq_snapshot_cash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fx_rate_id: Mapped[int | None] = mapped_column(
        ForeignKey("fx_rate_observations.id", ondelete="RESTRICT")
    )
    value_in_base: Mapped[Decimal | None] = mapped_column(MONEY)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "source_key", name="uq_transaction_source"),
        CheckConstraint("quantity >= 0", name="ck_transaction_quantity_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    traded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    gross_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    fee: Mapped[Decimal | None] = mapped_column(MONEY)
    tax: Mapped[Decimal | None] = mapped_column(MONEY)
    net_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class RecurringBuyPlan(TimestampMixin, Base):
    __tablename__ = "recurring_buy_plans"
    __table_args__ = (
        UniqueConstraint("plan_key", name="uq_recurring_plan_key"),
        CheckConstraint("daily_amount > 0", name="ck_recurring_daily_amount_positive"),
        CheckConstraint(
            "target_weight IS NULL OR (target_weight >= 0 AND target_weight <= 1)",
            name="ck_recurring_target_weight",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_key: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="MANUAL")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    daily_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_weight: Mapped[Decimal | None] = mapped_column(RATIO)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Seoul")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    memo: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class RecurringBuyRevision(Base):
    __tablename__ = "recurring_buy_revisions"
    __table_args__ = (UniqueConstraint("plan_id", "revision_no", name="uq_plan_revision_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_buy_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    change_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    daily_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_weight: Mapped[Decimal | None] = mapped_column(RATIO)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class RecurringBuyOccurrence(Base):
    __tablename__ = "recurring_buy_occurrences"
    __table_args__ = (
        UniqueConstraint("plan_id", "local_due_date", name="uq_plan_due_date"),
        CheckConstraint(
            "status IN ('PENDING','FILLED','PARTIAL','MISSED','UNKNOWN','SKIPPED')",
            name="ck_recurring_occurrence_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_buy_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_revision_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_buy_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    local_due_date: Mapped[date] = mapped_column(nullable=False)
    planned_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    actual_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class TargetAllocation(TimestampMixin, Base):
    __tablename__ = "target_allocations"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "scope_type", "scope_key", name="uq_target_allocation"),
        CheckConstraint("target_weight >= 0 AND target_weight <= 1", name="ck_target_weight"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_weight: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class InvestmentThesis(TimestampMixin, Base):
    __tablename__ = "investment_theses"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "instrument_id", name="uq_portfolio_instrument_thesis"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    current_revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ThesisRevision(Base):
    __tablename__ = "thesis_revisions"
    __table_args__ = (
        UniqueConstraint("thesis_id", "revision_no", name="uq_thesis_revision_no"),
        UniqueConstraint("thesis_id", "revision_key", name="uq_thesis_revision_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(
        ForeignKey("investment_theses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_key: Mapped[str] = mapped_column(String(128), nullable=False)
    thesis_text: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ThesisAssessment(Base):
    __tablename__ = "thesis_assessments"
    __table_args__ = (
        UniqueConstraint(
            "thesis_revision_id",
            "assessment_key",
            "analyzer_version",
            name="uq_thesis_assessment",
        ),
        CheckConstraint(
            "verdict IN ('STRENGTHENED','MAINTAINED','WEAKENED','BROKEN','UNKNOWN')",
            name="ck_thesis_assessment_verdict",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_revision_id: Mapped[int] = mapped_column(
        ForeignKey("thesis_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessment_key: Mapped[str] = mapped_column(String(128), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(48), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CompanyScore(Base):
    __tablename__ = "company_scores"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "model_version", "input_hash", name="uq_company_score_input"
        ),
        CheckConstraint("total_score >= 0 AND total_score <= 100", name="ck_company_score_total"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    fundamental_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    valuation_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    growth_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    moat_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    risk_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    total_score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    model_version: Mapped[str] = mapped_column(String(48), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class PortfolioScore(Base):
    __tablename__ = "portfolio_scores"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "instrument_id",
            "model_version",
            "input_hash",
            name="uq_portfolio_score_input",
        ),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_portfolio_score_total"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)
    current_weight: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    target_weight: Mapped[Decimal | None] = mapped_column(RATIO)
    model_version: Mapped[str] = mapped_column(String(48), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class MarketCacheEntry(TimestampMixin, Base):
    __tablename__ = "market_cache_entries"
    __table_args__ = (
        UniqueConstraint("provider", "namespace", "cache_key", name="uq_market_cache_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    namespace: Mapped[str] = mapped_column(String(48), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    stale_until: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MarketFetchAttempt(Base):
    __tablename__ = "market_fetch_attempts"
    __table_args__ = (
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_fetch_latency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    namespace: Mapped[str] = mapped_column(String(48), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    safe_error_type: Mapped[str | None] = mapped_column(String(96))
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_report_idempotency_key"),
        CheckConstraint("status IN ('CLAIMED','READY','FAILED')", name="ck_report_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    report_type: Mapped[str] = mapped_column(String(24), nullable=False)
    period_key: Mapped[str] = mapped_column(String(64), nullable=False)
    audience_fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="CLAIMED")
    input_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    safe_error_type: Mapped[str | None] = mapped_column(String(96))
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ReportDelivery(Base):
    __tablename__ = "report_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "channel",
            "destination_fingerprint",
            name="uq_report_delivery_destination",
        ),
        CheckConstraint(
            "status IN ('PENDING','SENT','FAILED','UNKNOWN')",
            name="ck_report_delivery_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    destination_fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_type: Mapped[str | None] = mapped_column(String(96))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SchemaMigration(Base):
    """Applied application schema migration.

    This table is bootstrapped separately from the versioned migrations so an
    existing unversioned account-bot database can be identified without being
    modified during normal application startup.
    """

    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class PortfolioImportBatch(Base):
    """One local workbook import snapshot without retaining a filesystem path."""

    __tablename__ = "portfolio_imports"
    __table_args__ = (
        UniqueConstraint("import_key", name="uq_portfolio_import_key"),
        CheckConstraint("status IN ('READY','BLOCKED')", name="ck_portfolio_import_status"),
        CheckConstraint("source_file_size >= 0", name="ck_portfolio_import_file_size"),
        CheckConstraint("marker_count >= 0", name="ck_portfolio_import_marker_count"),
        CheckConstraint("position_count >= 0", name="ck_portfolio_import_position_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_key: Mapped[str] = mapped_column(String(64), nullable=False)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="SET NULL"), index=True
    )
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    marker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    marker_rows: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_ticker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_ticker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost_basis_usd: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    total_import_market_value_usd: Mapped[Decimal] = mapped_column(
        IMPORT_DECIMAL, nullable=False
    )
    total_import_profit_usd: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    calculated_import_return_percent: Mapped[Decimal | None] = mapped_column(IMPORT_DECIMAL)
    daily_contribution_krw: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RawPortfolioImportRow(Base):
    """Lossless textual snapshot of a workbook row and its parser classification."""

    __tablename__ = "raw_portfolio_rows"
    __table_args__ = (
        UniqueConstraint("import_id", "sheet_name", "row_number", name="uq_raw_import_row"),
        CheckConstraint("row_number > 0", name="ck_raw_import_row_number"),
        CheckConstraint(
            "parse_status IN ('BLANK','HEADER','MARKER','VALID','INVALID')",
            name="ck_raw_import_parse_status",
        ),
        CheckConstraint(
            "source_input_type IS NULL OR source_input_type IN "
            "('TOTAL_POSITION_VALUE','PER_SHARE_PRICE')",
            name="ck_raw_import_input_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_ticker: Mapped[str | None] = mapped_column(Text)
    raw_quantity: Mapped[str | None] = mapped_column(Text)
    raw_purchase_value: Mapped[str | None] = mapped_column(Text)
    raw_market_value: Mapped[str | None] = mapped_column(Text)
    raw_provided_return_percent: Mapped[str | None] = mapped_column(Text)
    raw_daily_contribution_krw: Mapped[str | None] = mapped_column(Text)
    source_section_id: Mapped[str | None] = mapped_column(String(64))
    source_account_alias: Mapped[str | None] = mapped_column(String(64))
    source_input_type: Mapped[str | None] = mapped_column(String(32))
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_cells: Mapped[list[str | None]] = mapped_column(JSON, nullable=False, default=list)
    warning_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NormalizedPortfolioImportPosition(Base):
    """A position normalized to total USD amounts at workbook import time."""

    __tablename__ = "normalized_import_positions"
    __table_args__ = (
        UniqueConstraint("raw_row_id", name="uq_normalized_import_raw_row"),
        CheckConstraint(
            "CAST(quantity AS NUMERIC) >= 0", name="ck_normalized_import_quantity"
        ),
        CheckConstraint(
            "CAST(total_cost_basis_usd AS NUMERIC) >= 0",
            name="ck_normalized_import_cost",
        ),
        CheckConstraint(
            "CAST(import_market_value_usd AS NUMERIC) >= 0",
            name="ck_normalized_import_market_value",
        ),
        CheckConstraint(
            "CAST(daily_contribution_krw AS NUMERIC) >= 0",
            name="ck_normalized_import_contribution",
        ),
        CheckConstraint(
            "source_input_type IN ('TOTAL_POSITION_VALUE','PER_SHARE_PRICE')",
            name="ck_normalized_import_input_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_row_id: Mapped[int] = mapped_column(
        ForeignKey("raw_portfolio_rows.id", ondelete="CASCADE"), nullable=False
    )
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    total_cost_basis_usd: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    import_market_value_usd: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    average_cost_per_share_usd: Mapped[Decimal | None] = mapped_column(IMPORT_DECIMAL)
    import_current_price_usd: Mapped[Decimal | None] = mapped_column(IMPORT_DECIMAL)
    import_profit_usd: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    calculated_import_return_percent: Mapped[Decimal | None] = mapped_column(IMPORT_DECIMAL)
    provided_return_percent: Mapped[Decimal | None] = mapped_column(IMPORT_DECIMAL)
    daily_contribution_krw: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    source_section_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_account_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    source_input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class SavedInvestmentScenario(TimestampMixin, Base):
    """A named, durable snapshot of a daily-buy strategy."""

    __tablename__ = "saved_investment_scenarios"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "name", name="uq_saved_scenario_portfolio_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class SavedScenarioPlan(Base):
    """One exact Decimal contribution instruction inside a saved scenario."""

    __tablename__ = "saved_scenario_plans"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id",
            "market",
            "ticker",
            "asset_type",
            "instrument_currency",
            name="uq_saved_scenario_instrument",
        ),
        CheckConstraint(
            "CAST(configured_amount_krw AS NUMERIC) >= 0",
            name="ck_saved_scenario_amount_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("saved_investment_scenarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    configured_amount_krw: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class MarketPriceSnapshot(Base):
    """Provider-attributed price observation reserved for later live-price phases."""

    __tablename__ = "market_price_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider", "as_of", name="uq_market_price_observation"
        ),
        CheckConstraint("CAST(price AS NUMERIC) > 0", name="ck_market_price_positive"),
        CheckConstraint(
            "source_kind IN ('IMPORT','LIVE','CACHE')", name="ck_market_price_source_kind"
        ),
        Index("ix_market_price_instrument_as_of", "instrument_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price: Mapped[Decimal] = mapped_column(IMPORT_DECIMAL, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
