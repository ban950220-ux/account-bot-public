"""Repository contracts and a SQLite implementation with atomic idempotency."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import Select, delete, desc, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Database, DatabaseConfigurationError
from .models import (
    BrokerAccount,
    CashSnapshot,
    FxRateObservation,
    Instrument,
    InvestmentThesis,
    MarketCacheEntry,
    MarketFetchAttempt,
    MarketPriceSnapshot,
    Portfolio,
    PortfolioSnapshot,
    PositionSnapshot,
    RecurringBuyOccurrence,
    RecurringBuyPlan,
    RecurringBuyRevision,
    Report,
    ReportDelivery,
    SavedInvestmentScenario,
    SavedScenarioPlan,
    SnapshotSource,
    TargetAllocation,
    ThesisAssessment,
    ThesisRevision,
    Transaction,
    utc_now,
)


class RepositoryValidationError(ValueError):
    """Raised before unsafe or malformed data can reach persistent storage."""


_IDENTIFIER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,159}$")
_SAFE_ERROR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$")
_ASSET_TYPES = frozenset({"EQUITY", "ETF", "FUND", "BOND", "CASH", "OTHER"})
_FORBIDDEN_JSON_KEY_FRAGMENTS = (
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "app_key",
    "app_secret",
    "client_secret",
    "password",
    "private_key",
    "account_number",
    "account_no",
    "raw_payload",
    "raw_response",
    "credential",
    "secret",
    "token",
)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _identifier(value: str, field_name: str, *, max_length: int = 160) -> str:
    normalized = value.strip().upper()
    if len(normalized) > max_length or not _IDENTIFIER_RE.fullmatch(normalized):
        raise RepositoryValidationError(f"{field_name} contains unsupported characters")
    return normalized


def _currency(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise RepositoryValidationError("currency must be a three-letter code")
    return normalized


def _asset_type(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _ASSET_TYPES:
        raise RepositoryValidationError("unsupported asset_type")
    return normalized


def _decimal(
    value: Decimal | int | str | float,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, float) and not math.isfinite(value):
        raise RepositoryValidationError(f"{field_name} must be finite")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RepositoryValidationError(f"{field_name} must be decimal-compatible") from exc
    if not parsed.is_finite():
        raise RepositoryValidationError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise RepositoryValidationError(f"{field_name} is below its minimum")
    if maximum is not None and parsed > maximum:
        raise RepositoryValidationError(f"{field_name} is above its maximum")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    """Return an exponent-insensitive representation for idempotency hashes."""

    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _safe_error_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not _SAFE_ERROR_RE.fullmatch(normalized):
        raise RepositoryValidationError(
            "safe_error_type must be a class/category name without a message"
        )
    return normalized


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_fingerprint(value: str, *, pepper: str, namespace: str) -> str:
    """Derive a stable opaque reference without persisting the original value."""

    if not value or not pepper:
        raise RepositoryValidationError("fingerprint input and pepper must be non-empty")
    safe_namespace = re.sub(r"[^a-z0-9_]", "_", namespace.strip().lower()).strip("_")
    if not safe_namespace or len(safe_namespace) > 31:
        raise RepositoryValidationError("fingerprint namespace is invalid")
    digest = hmac.new(
        pepper.encode("utf-8"),
        f"{safe_namespace}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{safe_namespace}_{digest}"


def _require_fingerprint(value: str, field_name: str) -> str:
    if not _FINGERPRINT_RE.fullmatch(value):
        raise RepositoryValidationError(
            f"{field_name} must be an opaque fingerprint, not a raw identifier"
        )
    return value


def _normalize_json(value: Any, *, path: str = "$", depth: int = 0) -> Any:
    if depth > 12:
        raise RepositoryValidationError("normalized cache data is too deeply nested")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RepositoryValidationError("normalized cache data contains non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise RepositoryValidationError("normalized cache data contains non-finite decimal")
        return str(value)
    if isinstance(value, datetime):
        return _aware_utc(value, path).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise RepositoryValidationError("normalized cache keys must be strings")
            canonical_key = key.strip().lower().replace("-", "_")
            if any(fragment in canonical_key for fragment in _FORBIDDEN_JSON_KEY_FRAGMENTS):
                raise RepositoryValidationError(
                    "normalized cache data contains a forbidden sensitive/raw field"
                )
            normalized[key] = _normalize_json(child, path=f"{path}.{key}", depth=depth + 1)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json(child, path=f"{path}[]", depth=depth + 1) for child in value]
    raise RepositoryValidationError("normalized cache data contains an unsupported type")


def normalize_cache_data(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    normalized = _normalize_json(value)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > 262_144:
        raise RepositoryValidationError("normalized cache data exceeds 256 KiB")
    return normalized, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheLookup:
    data: Mapping[str, Any]
    provider: str
    fetched_at: datetime
    expires_at: datetime
    is_stale: bool
    content_hash: str


@dataclass(frozen=True, slots=True)
class FxRateLookup:
    observation_id: int | None
    rate: Decimal
    provider: str
    effective_at: datetime
    is_stale: bool


@dataclass(frozen=True, slots=True)
class MarketPriceLookup:
    price: Decimal
    currency: str
    provider: str
    as_of: datetime
    fetched_at: datetime
    is_stale: bool


@dataclass(frozen=True, slots=True)
class ClaimResult:
    record_id: int
    created: bool
    state: str | None = None


@dataclass(frozen=True, slots=True)
class RecurringBuyView:
    plan: RecurringBuyPlan
    market: str
    ticker: str
    instrument_currency: str
    asset_type: str
    company_name: str | None


@dataclass(frozen=True, slots=True)
class SavedScenarioItem:
    market: str
    ticker: str
    asset_type: str
    instrument_currency: str
    configured_amount_krw: Decimal
    enabled: bool


@dataclass(frozen=True, slots=True)
class SavedScenarioView:
    scenario_id: int
    name: str
    created_at: datetime
    updated_at: datetime
    items: tuple[SavedScenarioItem, ...]


class PortfolioRepository(ABC):
    """Storage contract used by services, independent of SQLAlchemy details."""

    @abstractmethod
    async def upsert_recurring_buy(self, **kwargs: Any) -> RecurringBuyPlan: ...

    @abstractmethod
    async def append_investment_thesis(self, **kwargs: Any) -> ThesisRevision: ...

    @abstractmethod
    async def record_thesis_assessment(self, **kwargs: Any) -> ThesisAssessment: ...

    @abstractmethod
    async def put_market_cache(self, **kwargs: Any) -> MarketCacheEntry: ...

    @abstractmethod
    async def get_market_cache(self, **kwargs: Any) -> CacheLookup | None: ...

    @abstractmethod
    async def add_fx_rate(self, **kwargs: Any) -> FxRateObservation: ...

    @abstractmethod
    async def claim_report(self, **kwargs: Any) -> ClaimResult: ...

    @abstractmethod
    async def save_investment_scenario(self, **kwargs: Any) -> SavedScenarioView: ...

    @abstractmethod
    async def list_investment_scenarios(self) -> list[SavedScenarioView]: ...

    @abstractmethod
    async def delete_investment_scenario(self, scenario_id: int) -> bool: ...


class SQLitePortfolioRepository(PortfolioRepository):
    """SQLite repository; dialect-specific upserts are confined to this class."""

    def __init__(self, database: Database, *, portfolio_key: str = "default") -> None:
        if database.backend != "sqlite+aiosqlite":
            raise DatabaseConfigurationError("SQLitePortfolioRepository requires sqlite+aiosqlite")
        self.database = database
        self.portfolio_key = portfolio_key.strip().lower()
        if not self.portfolio_key or len(self.portfolio_key) > 64:
            raise RepositoryValidationError("portfolio_key is invalid")

    async def initialize_default_portfolio(self) -> Portfolio:
        """Explicitly create/read the logical root used by all repository writes."""

        async with self.database.transaction() as session:
            return await self._portfolio(session)

    async def _portfolio(self, session: AsyncSession) -> Portfolio:
        statement = (
            sqlite_insert(Portfolio)
            .values(
                key=self.portfolio_key,
                name="Primary Portfolio",
                base_currency="KRW",
                timezone_name="Asia/Seoul",
                enabled=True,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        await session.execute(statement)
        return (
            await session.execute(select(Portfolio).where(Portfolio.key == self.portfolio_key))
        ).scalar_one()

    async def _instrument(
        self,
        session: AsyncSession,
        *,
        market: str,
        ticker: str,
        currency: str,
        asset_type: str = "EQUITY",
        company_name: str | None = None,
    ) -> Instrument:
        market_value = _identifier(market, "market", max_length=24)
        ticker_value = _identifier(ticker, "ticker", max_length=32)
        currency_value = _currency(currency)
        asset_type_value = _asset_type(asset_type)
        values: dict[str, Any] = {
            "market": market_value,
            "ticker": ticker_value,
            "asset_type": asset_type_value,
            "currency": currency_value,
        }
        if company_name:
            values["company_name"] = company_name.strip()[:200]
        existing = (
            await session.execute(
                select(Instrument).where(
                    Instrument.market == market_value,
                    Instrument.ticker == ticker_value,
                    Instrument.asset_type == asset_type_value,
                    Instrument.currency == currency_value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if company_name:
                existing.company_name = values["company_name"]
            existing.updated_at = utc_now()
            return existing
        statement = sqlite_insert(Instrument).values(**values)
        update_values: dict[str, Any] = {"updated_at": utc_now()}
        if company_name:
            update_values["company_name"] = statement.excluded.company_name
        try:
            async with session.begin_nested():
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["market", "ticker", "asset_type", "currency"],
                        set_=update_values,
                    )
                )
        except IntegrityError:
            # Older deployed databases used only (market, ticker) as the
            # unique key. Fall back only after that legacy constraint rejects
            # the full identity, preserving distinct modern identities.
            legacy = (
                await session.execute(
                    select(Instrument).where(
                        Instrument.market == market_value,
                        Instrument.ticker == ticker_value,
                    )
                )
            ).scalars().first()
            if legacy is None:
                raise
            if legacy.asset_type == "EQUITY" and asset_type_value != "EQUITY":
                legacy.asset_type = asset_type_value
            if company_name:
                legacy.company_name = values["company_name"]
            legacy.updated_at = utc_now()
            return legacy
        return (
            await session.execute(
                select(Instrument).where(
                    Instrument.market == market_value,
                    Instrument.ticker == ticker_value,
                    Instrument.asset_type == asset_type_value,
                    Instrument.currency == currency_value,
                )
            )
        ).scalar_one()

    async def upsert_account(
        self,
        *,
        broker: str,
        account_fingerprint: str,
        base_currency: str,
        masked_label: str | None = None,
        enabled: bool = True,
    ) -> BrokerAccount:
        broker_value = _identifier(broker, "broker", max_length=24)
        fingerprint = _require_fingerprint(account_fingerprint, "account_fingerprint")
        currency_value = _currency(base_currency)
        label = masked_label.strip()[:32] if masked_label else None
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            statement = sqlite_insert(BrokerAccount).values(
                portfolio_id=portfolio.id,
                broker=broker_value,
                account_fingerprint=fingerprint,
                masked_label=label,
                base_currency=currency_value,
                enabled=enabled,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["broker", "account_fingerprint"],
                    set_={
                        "portfolio_id": portfolio.id,
                        "masked_label": label,
                        "base_currency": currency_value,
                        "enabled": enabled,
                        "updated_at": utc_now(),
                    },
                )
            )
            return (
                await session.execute(
                    select(BrokerAccount).where(
                        BrokerAccount.broker == broker_value,
                        BrokerAccount.account_fingerprint == fingerprint,
                    )
                )
            ).scalar_one()

    async def upsert_recurring_buy(
        self,
        *,
        plan_key: str,
        market: str,
        ticker: str,
        daily_amount: Decimal | int | str | float,
        currency: str,
        instrument_currency: str | None = None,
        asset_type: str = "EQUITY",
        target_weight: Decimal | int | str | float | None = None,
        enabled: bool = True,
        account_id: int | None = None,
        source: str = "MANUAL",
        timezone_name: str = "Asia/Seoul",
        started_at: datetime | None = None,
        memo: str | None = None,
        effective_at: datetime | None = None,
    ) -> RecurringBuyPlan:
        normalized_plan_key = plan_key.strip()
        if not normalized_plan_key or len(normalized_plan_key) > 160:
            raise RepositoryValidationError("plan_key is invalid")
        amount = _decimal(daily_amount, "daily_amount", minimum=Decimal("0.00000001"))
        weight = (
            None
            if target_weight is None
            else _decimal(
                target_weight,
                "target_weight",
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            )
        )
        currency_value = _currency(currency)
        instrument_currency_value = _currency(instrument_currency or currency_value)
        source_value = _identifier(source, "source", max_length=24)
        requested_start = None if started_at is None else _aware_utc(started_at, "started_at")
        effective = _aware_utc(effective_at or utc_now(), "effective_at")
        memo_value = memo.strip()[:2_000] if memo and memo.strip() else None

        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            instrument = await self._instrument(
                session,
                market=market,
                ticker=ticker,
                currency=instrument_currency_value,
                asset_type=asset_type,
            )
            plan = (
                await session.execute(
                    select(RecurringBuyPlan).where(RecurringBuyPlan.plan_key == normalized_plan_key)
                )
            ).scalar_one_or_none()
            start = requested_start or (plan.started_at if plan is not None else effective)
            canonical = json.dumps(
                {
                    "instrument_id": instrument.id,
                    "account_id": account_id,
                    "source": source_value,
                    "enabled": bool(enabled),
                    "daily_amount": _canonical_decimal(amount),
                    "currency": currency_value,
                    "target_weight": (None if weight is None else _canonical_decimal(weight)),
                    "timezone_name": timezone_name,
                    "started_at": start.isoformat(),
                    "memo": memo_value,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            change_hash = _hash_text(canonical)

            if plan is None:
                plan = RecurringBuyPlan(
                    portfolio_id=portfolio.id,
                    plan_key=normalized_plan_key,
                    instrument_id=instrument.id,
                    account_id=account_id,
                    source=source_value,
                    enabled=bool(enabled),
                    daily_amount=amount,
                    currency=currency_value,
                    target_weight=weight,
                    timezone_name=timezone_name,
                    started_at=start,
                    memo=memo_value,
                    version=1,
                )
                session.add(plan)
                await session.flush()
                revision_no = 1
            else:
                prior = (
                    await session.execute(
                        select(RecurringBuyRevision).where(
                            RecurringBuyRevision.plan_id == plan.id,
                            RecurringBuyRevision.revision_no == plan.version,
                            RecurringBuyRevision.change_hash == change_hash,
                        )
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    return plan
                revision_no = plan.version + 1
                plan.portfolio_id = portfolio.id
                plan.instrument_id = instrument.id
                plan.account_id = account_id
                plan.source = source_value
                plan.enabled = bool(enabled)
                plan.daily_amount = amount
                plan.currency = currency_value
                plan.target_weight = weight
                plan.timezone_name = timezone_name
                plan.started_at = start
                plan.memo = memo_value
                plan.version = revision_no
                plan.updated_at = utc_now()

            session.add(
                RecurringBuyRevision(
                    plan_id=plan.id,
                    revision_no=revision_no,
                    change_hash=change_hash,
                    instrument_id=instrument.id,
                    account_id=account_id,
                    source=source_value,
                    enabled=bool(enabled),
                    daily_amount=amount,
                    currency=currency_value,
                    target_weight=weight,
                    timezone_name=timezone_name,
                    started_at=start,
                    effective_from=effective,
                    memo=memo_value,
                )
            )
            await session.flush()
            return plan

    async def list_recurring_buys(self, *, enabled_only: bool = False) -> list[RecurringBuyPlan]:
        async with self.database.session() as session:
            statement: Select[tuple[RecurringBuyPlan]] = select(RecurringBuyPlan).order_by(
                RecurringBuyPlan.plan_key
            )
            if enabled_only:
                statement = statement.where(RecurringBuyPlan.enabled.is_(True))
            return list((await session.execute(statement)).scalars().all())

    async def get_recurring_buy(self, plan_key: str) -> RecurringBuyPlan | None:
        async with self.database.session() as session:
            return (
                await session.execute(
                    select(RecurringBuyPlan).where(RecurringBuyPlan.plan_key == plan_key.strip())
                )
            ).scalar_one_or_none()

    async def list_recurring_buy_views(
        self, *, enabled_only: bool = False
    ) -> list[RecurringBuyView]:
        async with self.database.session() as session:
            statement = (
                select(RecurringBuyPlan, Instrument)
                .join(Instrument, RecurringBuyPlan.instrument_id == Instrument.id)
                .order_by(Instrument.market, Instrument.ticker, RecurringBuyPlan.plan_key)
            )
            if enabled_only:
                statement = statement.where(RecurringBuyPlan.enabled.is_(True))
            rows = (await session.execute(statement)).all()
            return [
                RecurringBuyView(
                    plan=plan,
                    market=instrument.market,
                    ticker=instrument.ticker,
                    instrument_currency=instrument.currency,
                    asset_type=instrument.asset_type,
                    company_name=instrument.company_name,
                )
                for plan, instrument in rows
            ]

    async def get_recurring_buy_view(self, plan_key: str) -> RecurringBuyView | None:
        async with self.database.session() as session:
            row = (
                await session.execute(
                    select(RecurringBuyPlan, Instrument)
                    .join(Instrument, RecurringBuyPlan.instrument_id == Instrument.id)
                    .where(RecurringBuyPlan.plan_key == plan_key.strip())
                )
            ).one_or_none()
            if row is None:
                return None
            plan, instrument = row
            return RecurringBuyView(
                plan=plan,
                market=instrument.market,
                ticker=instrument.ticker,
                instrument_currency=instrument.currency,
                asset_type=instrument.asset_type,
                company_name=instrument.company_name,
            )

    @staticmethod
    def _saved_scenario_view(
        scenario: SavedInvestmentScenario,
        plans: Sequence[SavedScenarioPlan],
    ) -> SavedScenarioView:
        return SavedScenarioView(
            scenario_id=scenario.id,
            name=scenario.name,
            created_at=scenario.created_at,
            updated_at=scenario.updated_at,
            items=tuple(
                SavedScenarioItem(
                    market=plan.market,
                    ticker=plan.ticker,
                    asset_type=plan.asset_type,
                    instrument_currency=plan.instrument_currency,
                    configured_amount_krw=plan.configured_amount_krw,
                    enabled=plan.enabled,
                )
                for plan in plans
            ),
        )

    async def save_investment_scenario(
        self,
        *,
        name: str,
        items: Sequence[Mapping[str, Any]],
    ) -> SavedScenarioView:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise RepositoryValidationError("scenario name must contain 1 to 80 characters")
        if any(ord(character) < 32 for character in normalized_name):
            raise RepositoryValidationError("scenario name contains control characters")
        normalized_items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in items:
            market = _identifier(str(item["market"]), "market", max_length=24)
            ticker = _identifier(str(item["ticker"]), "ticker", max_length=32)
            asset_type = _asset_type(str(item["asset_type"]))
            currency = _currency(str(item["instrument_currency"]))
            identity = (market, ticker, asset_type, currency)
            if identity in seen:
                raise RepositoryValidationError("scenario contains a duplicate instrument")
            seen.add(identity)
            raw_amount = item["configured_amount_krw"]
            if isinstance(raw_amount, float):
                raise RepositoryValidationError(
                    "configured_amount_krw must not use binary float"
                )
            normalized_items.append(
                {
                    "market": market,
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "instrument_currency": currency,
                    "configured_amount_krw": _decimal(
                        raw_amount,
                        "configured_amount_krw",
                        minimum=Decimal(),
                    ),
                    "enabled": bool(item["enabled"]),
                }
            )
        if not normalized_items:
            raise RepositoryValidationError("scenario must contain at least one investment plan")
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            changed_at = utc_now()
            statement = sqlite_insert(SavedInvestmentScenario).values(
                portfolio_id=portfolio.id,
                name=normalized_name,
                created_at=changed_at,
                updated_at=changed_at,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["portfolio_id", "name"],
                    set_={"updated_at": changed_at},
                )
            )
            scenario = (
                await session.execute(
                select(SavedInvestmentScenario).where(
                    SavedInvestmentScenario.portfolio_id == portfolio.id,
                    SavedInvestmentScenario.name == normalized_name,
                )
                )
            ).scalar_one()
            await session.execute(
                delete(SavedScenarioPlan).where(
                    SavedScenarioPlan.scenario_id == scenario.id
                )
            )
            plans = [
                SavedScenarioPlan(scenario_id=scenario.id, **item)
                for item in normalized_items
            ]
            session.add_all(plans)
            await session.flush()
            return self._saved_scenario_view(scenario, plans)

    async def list_investment_scenarios(self) -> list[SavedScenarioView]:
        async with self.database.session() as session:
            portfolio = await session.scalar(
                select(Portfolio).where(Portfolio.key == self.portfolio_key)
            )
            if portfolio is None:
                return []
            scenarios = list(
                (
                    await session.scalars(
                        select(SavedInvestmentScenario)
                        .where(SavedInvestmentScenario.portfolio_id == portfolio.id)
                        .order_by(SavedInvestmentScenario.updated_at.desc())
                    )
                ).all()
            )
            if not scenarios:
                return []
            plans = list(
                (
                    await session.scalars(
                        select(SavedScenarioPlan)
                        .where(
                            SavedScenarioPlan.scenario_id.in_(
                                scenario.id for scenario in scenarios
                            )
                        )
                        .order_by(SavedScenarioPlan.ticker)
                    )
                ).all()
            )
            by_scenario: dict[int, list[SavedScenarioPlan]] = {}
            for plan in plans:
                by_scenario.setdefault(plan.scenario_id, []).append(plan)
            return [
                self._saved_scenario_view(
                    scenario, by_scenario.get(scenario.id, [])
                )
                for scenario in scenarios
            ]

    async def get_investment_scenario(
        self, scenario_id: int
    ) -> SavedScenarioView | None:
        async with self.database.session() as session:
            portfolio = await session.scalar(
                select(Portfolio).where(Portfolio.key == self.portfolio_key)
            )
            if portfolio is None:
                return None
            scenario = await session.scalar(
                select(SavedInvestmentScenario).where(
                    SavedInvestmentScenario.id == scenario_id,
                    SavedInvestmentScenario.portfolio_id == portfolio.id,
                )
            )
            if scenario is None:
                return None
            plans = list(
                (
                    await session.scalars(
                        select(SavedScenarioPlan)
                        .where(SavedScenarioPlan.scenario_id == scenario.id)
                        .order_by(SavedScenarioPlan.ticker)
                    )
                ).all()
            )
            return self._saved_scenario_view(scenario, plans)

    async def delete_investment_scenario(self, scenario_id: int) -> bool:
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            result = await session.execute(
                delete(SavedInvestmentScenario).where(
                    SavedInvestmentScenario.id == scenario_id,
                    SavedInvestmentScenario.portfolio_id == portfolio.id,
                )
            )
            return bool(result.rowcount)

    async def set_recurring_buy_enabled(
        self,
        *,
        plan_key: str,
        enabled: bool,
        effective_at: datetime | None = None,
    ) -> RecurringBuyPlan:
        effective = _aware_utc(effective_at or utc_now(), "effective_at")
        async with self.database.transaction() as session:
            plan = (
                await session.execute(
                    select(RecurringBuyPlan).where(RecurringBuyPlan.plan_key == plan_key.strip())
                )
            ).scalar_one_or_none()
            if plan is None:
                raise RepositoryValidationError("recurring plan does not exist")
            if plan.enabled == bool(enabled):
                return plan
            canonical = json.dumps(
                {
                    "instrument_id": plan.instrument_id,
                    "account_id": plan.account_id,
                    "source": plan.source,
                    "enabled": bool(enabled),
                    "daily_amount": _canonical_decimal(plan.daily_amount),
                    "currency": plan.currency,
                    "target_weight": (
                        None
                        if plan.target_weight is None
                        else _canonical_decimal(plan.target_weight)
                    ),
                    "timezone_name": plan.timezone_name,
                    "started_at": plan.started_at.isoformat(),
                    "memo": plan.memo,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            plan.version += 1
            plan.enabled = bool(enabled)
            plan.updated_at = utc_now()
            session.add(
                RecurringBuyRevision(
                    plan_id=plan.id,
                    revision_no=plan.version,
                    change_hash=_hash_text(canonical),
                    instrument_id=plan.instrument_id,
                    account_id=plan.account_id,
                    source=plan.source,
                    enabled=bool(enabled),
                    daily_amount=plan.daily_amount,
                    currency=plan.currency,
                    target_weight=plan.target_weight,
                    timezone_name=plan.timezone_name,
                    started_at=plan.started_at,
                    effective_from=effective,
                    memo=plan.memo,
                )
            )
            await session.flush()
            return plan

    async def ensure_recurring_occurrence(
        self, *, plan_id: int, local_due_date: date
    ) -> RecurringBuyOccurrence:
        async with self.database.transaction() as session:
            plan = await session.get(RecurringBuyPlan, plan_id)
            if plan is None:
                raise RepositoryValidationError("recurring plan does not exist")
            revision = (
                await session.execute(
                    select(RecurringBuyRevision).where(
                        RecurringBuyRevision.plan_id == plan.id,
                        RecurringBuyRevision.revision_no == plan.version,
                    )
                )
            ).scalar_one_or_none()
            if revision is None:
                raise RepositoryValidationError("recurring plan has no current revision")
            statement = (
                sqlite_insert(RecurringBuyOccurrence)
                .values(
                    plan_id=plan.id,
                    plan_revision_id=revision.id,
                    local_due_date=local_due_date,
                    planned_amount=plan.daily_amount,
                    currency=plan.currency,
                    status="PENDING" if plan.enabled else "SKIPPED",
                )
                .on_conflict_do_nothing(index_elements=["plan_id", "local_due_date"])
            )
            await session.execute(statement)
            return (
                await session.execute(
                    select(RecurringBuyOccurrence).where(
                        RecurringBuyOccurrence.plan_id == plan.id,
                        RecurringBuyOccurrence.local_due_date == local_due_date,
                    )
                )
            ).scalar_one()

    async def reconcile_recurring_occurrence(
        self,
        *,
        plan_id: int,
        local_due_date: date,
        status: str,
        actual_amount: Decimal | int | str | float | None = None,
        actual_quantity: Decimal | int | str | float | None = None,
        evaluated_at: datetime | None = None,
    ) -> RecurringBuyOccurrence:
        status_value = _identifier(status, "status", max_length=16)
        if status_value not in {"FILLED", "PARTIAL", "MISSED", "UNKNOWN", "SKIPPED"}:
            raise RepositoryValidationError("unsupported recurring occurrence status")
        occurrence = await self.ensure_recurring_occurrence(
            plan_id=plan_id, local_due_date=local_due_date
        )
        async with self.database.transaction() as session:
            stored = await session.get(RecurringBuyOccurrence, occurrence.id)
            if stored is None:
                raise RepositoryValidationError("recurring occurrence does not exist")
            stored.status = status_value
            stored.actual_amount = (
                None
                if actual_amount is None
                else _decimal(actual_amount, "actual_amount", minimum=Decimal("0"))
            )
            stored.actual_quantity = (
                None
                if actual_quantity is None
                else _decimal(actual_quantity, "actual_quantity", minimum=Decimal("0"))
            )
            stored.evaluated_at = _aware_utc(evaluated_at or utc_now(), "evaluated_at")
            await session.flush()
            return stored

    async def append_investment_thesis(
        self,
        *,
        revision_key: str,
        market: str,
        ticker: str,
        currency: str,
        thesis_text: str,
        effective_at: datetime | None = None,
    ) -> ThesisRevision:
        revision_key_value = revision_key.strip()
        text_value = thesis_text.strip()
        if not revision_key_value or len(revision_key_value) > 128:
            raise RepositoryValidationError("revision_key is invalid")
        if not text_value or len(text_value) > 20_000:
            raise RepositoryValidationError("thesis_text must be 1..20000 characters")
        effective = _aware_utc(effective_at or utc_now(), "effective_at")
        content_hash = _hash_text(text_value)
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            instrument = await self._instrument(
                session, market=market, ticker=ticker, currency=currency
            )
            await session.execute(
                sqlite_insert(InvestmentThesis)
                .values(
                    portfolio_id=portfolio.id,
                    instrument_id=instrument.id,
                    status="ACTIVE",
                    current_revision_no=0,
                )
                .on_conflict_do_nothing(index_elements=["portfolio_id", "instrument_id"])
            )
            thesis = (
                await session.execute(
                    select(InvestmentThesis).where(
                        InvestmentThesis.portfolio_id == portfolio.id,
                        InvestmentThesis.instrument_id == instrument.id,
                    )
                )
            ).scalar_one()
            existing = (
                await session.execute(
                    select(ThesisRevision).where(
                        ThesisRevision.thesis_id == thesis.id,
                        ThesisRevision.revision_key == revision_key_value,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            revision_no = thesis.current_revision_no + 1
            revision = ThesisRevision(
                thesis_id=thesis.id,
                revision_no=revision_no,
                revision_key=revision_key_value,
                thesis_text=text_value,
                effective_at=effective,
                content_hash=content_hash,
            )
            session.add(revision)
            thesis.current_revision_no = revision_no
            thesis.updated_at = utc_now()
            await session.flush()
            return revision

    async def get_current_investment_thesis(
        self, *, market: str, ticker: str
    ) -> ThesisRevision | None:
        market_value = _identifier(market, "market", max_length=24)
        ticker_value = _identifier(ticker, "ticker", max_length=32)
        async with self.database.session() as session:
            statement = (
                select(ThesisRevision)
                .join(InvestmentThesis, ThesisRevision.thesis_id == InvestmentThesis.id)
                .join(Instrument, InvestmentThesis.instrument_id == Instrument.id)
                .where(
                    Instrument.market == market_value,
                    Instrument.ticker == ticker_value,
                    ThesisRevision.revision_no == InvestmentThesis.current_revision_no,
                )
            )
            return (await session.execute(statement)).scalar_one_or_none()

    async def list_investment_thesis_history(
        self, *, market: str, ticker: str
    ) -> list[ThesisRevision]:
        market_value = _identifier(market, "market", max_length=24)
        ticker_value = _identifier(ticker, "ticker", max_length=32)
        async with self.database.session() as session:
            statement = (
                select(ThesisRevision)
                .join(InvestmentThesis, ThesisRevision.thesis_id == InvestmentThesis.id)
                .join(Instrument, InvestmentThesis.instrument_id == Instrument.id)
                .where(
                    Instrument.market == market_value,
                    Instrument.ticker == ticker_value,
                )
                .order_by(ThesisRevision.revision_no)
            )
            return list((await session.execute(statement)).scalars().all())

    async def record_thesis_assessment(
        self,
        *,
        thesis_revision_id: int,
        assessment_key: str,
        analyzer_version: str,
        input_hash: str,
        verdict: str,
        rationale: str,
        assessed_at: datetime | None = None,
    ) -> ThesisAssessment:
        key = assessment_key.strip()
        version = analyzer_version.strip()
        rationale_value = rationale.strip()
        if not key or len(key) > 128:
            raise RepositoryValidationError("assessment_key is invalid")
        if not version or len(version) > 48:
            raise RepositoryValidationError("analyzer_version is invalid")
        if not _SHA256_RE.fullmatch(input_hash):
            raise RepositoryValidationError("input_hash must be lowercase SHA-256")
        verdict_value = _identifier(verdict, "verdict", max_length=16)
        if verdict_value not in {
            "STRENGTHENED",
            "MAINTAINED",
            "WEAKENED",
            "BROKEN",
            "UNKNOWN",
        }:
            raise RepositoryValidationError("unsupported thesis assessment verdict")
        if not rationale_value or len(rationale_value) > 20_000:
            raise RepositoryValidationError("rationale must be 1..20000 characters")
        assessed = _aware_utc(assessed_at or utc_now(), "assessed_at")
        values = {
            "thesis_revision_id": thesis_revision_id,
            "assessment_key": key,
            "analyzer_version": version,
            "input_hash": input_hash,
            "verdict": verdict_value,
            "rationale": rationale_value,
            "assessed_at": assessed,
        }
        async with self.database.transaction() as session:
            if await session.get(ThesisRevision, thesis_revision_id) is None:
                raise RepositoryValidationError("thesis revision does not exist")
            await session.execute(
                sqlite_insert(ThesisAssessment)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "thesis_revision_id",
                        "assessment_key",
                        "analyzer_version",
                    ]
                )
            )
            return (
                await session.execute(
                    select(ThesisAssessment).where(
                        ThesisAssessment.thesis_revision_id == thesis_revision_id,
                        ThesisAssessment.assessment_key == key,
                        ThesisAssessment.analyzer_version == version,
                    )
                )
            ).scalar_one()

    async def add_thesis_assessment(self, **kwargs: Any) -> ThesisAssessment:
        """Alias for callers that use add-style repository naming."""

        return await self.record_thesis_assessment(**kwargs)

    async def set_investment_thesis(self, **kwargs: Any) -> ThesisRevision:
        """Service-friendly alias; revisions remain append-only and idempotent."""

        return await self.append_investment_thesis(**kwargs)

    async def put_market_cache(
        self,
        *,
        provider: str,
        namespace: str,
        cache_key: str,
        normalized_data: Mapping[str, Any],
        fetched_at: datetime,
        expires_at: datetime,
        stale_until: datetime | None = None,
        schema_version: int = 1,
        latency_ms: int | None = None,
    ) -> MarketCacheEntry:
        provider_value = _identifier(provider, "provider", max_length=48)
        namespace_value = _identifier(namespace, "namespace", max_length=48)
        cache_key_value = cache_key.strip()
        if not cache_key_value or len(cache_key_value) > 160:
            raise RepositoryValidationError("cache_key is invalid")
        if schema_version < 1:
            raise RepositoryValidationError("schema_version must be positive")
        if latency_ms is not None and latency_ms < 0:
            raise RepositoryValidationError("latency_ms must be nonnegative")
        normalized, content_hash = normalize_cache_data(normalized_data)
        fetched = _aware_utc(fetched_at, "fetched_at")
        expires = _aware_utc(expires_at, "expires_at")
        stale = None if stale_until is None else _aware_utc(stale_until, "stale_until")
        if expires <= fetched:
            raise RepositoryValidationError("expires_at must be after fetched_at")
        if stale is not None and stale < expires:
            raise RepositoryValidationError("stale_until must not precede expires_at")

        async with self.database.transaction() as session:
            statement = sqlite_insert(MarketCacheEntry).values(
                provider=provider_value,
                namespace=namespace_value,
                cache_key=cache_key_value,
                normalized_data=normalized,
                schema_version=schema_version,
                content_hash=content_hash,
                fetched_at=fetched,
                expires_at=expires,
                stale_until=stale,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["provider", "namespace", "cache_key"],
                    set_={
                        "normalized_data": statement.excluded.normalized_data,
                        "schema_version": statement.excluded.schema_version,
                        "content_hash": statement.excluded.content_hash,
                        "fetched_at": statement.excluded.fetched_at,
                        "expires_at": statement.excluded.expires_at,
                        "stale_until": statement.excluded.stale_until,
                        "updated_at": utc_now(),
                    },
                    where=statement.excluded.fetched_at >= MarketCacheEntry.fetched_at,
                )
            )
            session.add(
                MarketFetchAttempt(
                    provider=provider_value,
                    namespace=namespace_value,
                    cache_key=cache_key_value,
                    attempted_at=fetched,
                    success=True,
                    latency_ms=latency_ms,
                )
            )
            await session.flush()
            return (
                await session.execute(
                    select(MarketCacheEntry).where(
                        MarketCacheEntry.provider == provider_value,
                        MarketCacheEntry.namespace == namespace_value,
                        MarketCacheEntry.cache_key == cache_key_value,
                    )
                )
            ).scalar_one()

    async def record_market_fetch_failure(
        self,
        *,
        provider: str,
        namespace: str,
        cache_key: str,
        attempted_at: datetime,
        safe_error_type: str,
        latency_ms: int | None = None,
    ) -> None:
        cache_key_value = cache_key.strip()
        if not cache_key_value or len(cache_key_value) > 160:
            raise RepositoryValidationError("cache_key is invalid")
        if latency_ms is not None and latency_ms < 0:
            raise RepositoryValidationError("latency_ms must be nonnegative")
        async with self.database.transaction() as session:
            session.add(
                MarketFetchAttempt(
                    provider=_identifier(provider, "provider", max_length=48),
                    namespace=_identifier(namespace, "namespace", max_length=48),
                    cache_key=cache_key_value,
                    attempted_at=_aware_utc(attempted_at, "attempted_at"),
                    success=False,
                    safe_error_type=_safe_error_type(safe_error_type),
                    latency_ms=latency_ms,
                )
            )

    async def get_market_cache(
        self,
        *,
        provider: str,
        namespace: str,
        cache_key: str,
        now: datetime | None = None,
        allow_stale: bool = False,
    ) -> CacheLookup | None:
        current = _aware_utc(now or utc_now(), "now")
        async with self.database.session() as session:
            entry = (
                await session.execute(
                    select(MarketCacheEntry).where(
                        MarketCacheEntry.provider
                        == _identifier(provider, "provider", max_length=48),
                        MarketCacheEntry.namespace
                        == _identifier(namespace, "namespace", max_length=48),
                        MarketCacheEntry.cache_key == cache_key.strip(),
                    )
                )
            ).scalar_one_or_none()
            if entry is None:
                return None
            is_stale = current > entry.expires_at
            if is_stale and (
                not allow_stale or entry.stale_until is None or current > entry.stale_until
            ):
                return None
            return CacheLookup(
                data=dict(entry.normalized_data),
                provider=entry.provider,
                fetched_at=entry.fetched_at,
                expires_at=entry.expires_at,
                is_stale=is_stale,
                content_hash=entry.content_hash,
            )

    async def get_fresh_cache(self, **kwargs: Any) -> CacheLookup | None:
        kwargs["allow_stale"] = False
        return await self.get_market_cache(**kwargs)

    async def get_cache_with_stale_fallback(self, **kwargs: Any) -> CacheLookup | None:
        kwargs["allow_stale"] = True
        return await self.get_market_cache(**kwargs)

    async def add_fx_rate(
        self,
        *,
        observation_key: str,
        provider: str,
        base_currency: str,
        quote_currency: str,
        rate: Decimal | int | str | float,
        effective_at: datetime,
        retrieved_at: datetime | None = None,
    ) -> FxRateObservation:
        key = observation_key.strip()
        if not key or len(key) > 128:
            raise RepositoryValidationError("observation_key is invalid")
        values = {
            "observation_key": key,
            "provider": _identifier(provider, "provider", max_length=48),
            "base_currency": _currency(base_currency),
            "quote_currency": _currency(quote_currency),
            "rate": _decimal(rate, "rate", minimum=Decimal("0.000000000001")),
            "effective_at": _aware_utc(effective_at, "effective_at"),
            "retrieved_at": _aware_utc(retrieved_at or utc_now(), "retrieved_at"),
        }
        async with self.database.transaction() as session:
            await session.execute(
                sqlite_insert(FxRateObservation)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["observation_key"])
            )
            return (
                await session.execute(
                    select(FxRateObservation).where(FxRateObservation.observation_key == key)
                )
            ).scalar_one()

    async def get_fx_rate_at_or_before(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        as_of: datetime,
        max_age: timedelta,
        providers: Sequence[str] | None = None,
    ) -> FxRateLookup | None:
        base = _currency(base_currency)
        quote = _currency(quote_currency)
        target = _aware_utc(as_of, "as_of")
        if max_age <= timedelta(0):
            raise RepositoryValidationError("max_age must be positive")
        if base == quote:
            return FxRateLookup(None, Decimal("1"), "IDENTITY", target, False)
        async with self.database.session() as session:
            statement = select(FxRateObservation).where(
                FxRateObservation.base_currency == base,
                FxRateObservation.quote_currency == quote,
                FxRateObservation.effective_at <= target,
            )
            if providers:
                provider_values = [
                    _identifier(value, "provider", max_length=48) for value in providers
                ]
                statement = statement.where(FxRateObservation.provider.in_(provider_values))
            statement = statement.order_by(desc(FxRateObservation.effective_at)).limit(1)
            observation = (await session.execute(statement)).scalar_one_or_none()
            if observation is None:
                return None
            age = target - observation.effective_at
            if age > max_age:
                return None
            return FxRateLookup(
                observation_id=observation.id,
                rate=observation.rate,
                provider=observation.provider,
                effective_at=observation.effective_at,
                is_stale=age > timedelta(hours=24),
            )

    async def get_latest_fx_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        max_age: timedelta = timedelta(days=7),
        providers: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> FxRateLookup | None:
        return await self.get_fx_rate_at_or_before(
            base_currency=base_currency,
            quote_currency=quote_currency,
            as_of=now or utc_now(),
            max_age=max_age,
            providers=providers,
        )

    async def add_market_price(
        self,
        *,
        market: str,
        ticker: str,
        asset_type: str,
        currency: str,
        price: Decimal | str | int,
        as_of: datetime,
        provider: str,
        source_kind: str = "LIVE",
        stale: bool = False,
        fetched_at: datetime | None = None,
    ) -> MarketPriceSnapshot:
        observed = _aware_utc(as_of, "as_of")
        fetched = _aware_utc(fetched_at or utc_now(), "fetched_at")
        source = source_kind.strip().upper()
        if source not in {"IMPORT", "LIVE", "CACHE"}:
            raise RepositoryValidationError("source_kind is invalid")
        async with self.database.transaction() as session:
            instrument = await self._instrument(
                session,
                market=market,
                ticker=ticker,
                currency=currency,
                asset_type=asset_type,
            )
            statement = sqlite_insert(MarketPriceSnapshot).values(
                instrument_id=instrument.id,
                price=_decimal(price, "price", minimum=Decimal("0.000000000001")),
                currency=_currency(currency),
                as_of=observed,
                fetched_at=fetched,
                provider=_identifier(provider, "provider", max_length=48),
                source_kind=source,
                stale=bool(stale),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["instrument_id", "provider", "as_of"],
                    set_={
                        "price": statement.excluded.price,
                        "fetched_at": statement.excluded.fetched_at,
                        "source_kind": statement.excluded.source_kind,
                        "stale": statement.excluded.stale,
                    },
                )
            )
            return (
                await session.execute(
                    select(MarketPriceSnapshot).where(
                        MarketPriceSnapshot.instrument_id == instrument.id,
                        MarketPriceSnapshot.provider
                        == _identifier(provider, "provider", max_length=48),
                        MarketPriceSnapshot.as_of == observed,
                    )
                )
            ).scalar_one()

    async def claim_report(
        self,
        *,
        idempotency_key: str,
        report_type: str,
        period_key: str,
        audience_fingerprint: str,
        timezone_name: str = "Asia/Seoul",
        claimed_at: datetime | None = None,
    ) -> ClaimResult:
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise RepositoryValidationError("idempotency_key is invalid")
        audience = _require_fingerprint(audience_fingerprint, "audience_fingerprint")
        claimed = _aware_utc(claimed_at or utc_now(), "claimed_at")
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            statement = (
                sqlite_insert(Report)
                .values(
                    portfolio_id=portfolio.id,
                    idempotency_key=key,
                    report_type=_identifier(report_type, "report_type", max_length=24),
                    period_key=period_key.strip()[:64],
                    audience_fingerprint=audience,
                    timezone_name=timezone_name,
                    status="CLAIMED",
                    claimed_at=claimed,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            result = await session.execute(statement)
            report = (
                await session.execute(select(Report).where(Report.idempotency_key == key))
            ).scalar_one()
            return ClaimResult(
                record_id=report.id,
                created=result.rowcount == 1,
                state=report.status,
            )

    async def complete_report(
        self,
        *,
        report_id: int,
        input_hash: str,
        content: str,
        completed_at: datetime | None = None,
    ) -> Report:
        if not _SHA256_RE.fullmatch(input_hash):
            raise RepositoryValidationError("input_hash must be lowercase SHA-256")
        if not content:
            raise RepositoryValidationError("report content must not be empty")
        content_hash = _hash_text(content)
        async with self.database.transaction() as session:
            report = await session.get(Report, report_id)
            if report is None:
                raise RepositoryValidationError("report does not exist")
            report.input_hash = input_hash
            report.content_hash = content_hash
            report.status = "READY"
            report.safe_error_type = None
            report.completed_at = _aware_utc(completed_at or utc_now(), "completed_at")
            await session.flush()
            return report

    async def get_report_by_idempotency_key(self, key: str) -> Report | None:
        async with self.database.session() as session:
            return (
                await session.execute(select(Report).where(Report.idempotency_key == key.strip()))
            ).scalar_one_or_none()

    async def fail_report(self, *, report_id: int, safe_error_type: str) -> Report:
        async with self.database.transaction() as session:
            report = await session.get(Report, report_id)
            if report is None:
                raise RepositoryValidationError("report does not exist")
            report.status = "FAILED"
            report.safe_error_type = _safe_error_type(safe_error_type)
            report.completed_at = utc_now()
            await session.flush()
            return report

    async def claim_report_delivery(
        self,
        *,
        report_id: int,
        channel: str,
        destination_fingerprint: str,
    ) -> ClaimResult:
        destination = _require_fingerprint(destination_fingerprint, "destination_fingerprint")
        channel_value = _identifier(channel, "channel", max_length=24)
        async with self.database.transaction() as session:
            statement = (
                sqlite_insert(ReportDelivery)
                .values(
                    report_id=report_id,
                    channel=channel_value,
                    destination_fingerprint=destination,
                    status="PENDING",
                    attempt_count=0,
                )
                .on_conflict_do_nothing(
                    index_elements=["report_id", "channel", "destination_fingerprint"]
                )
            )
            result = await session.execute(statement)
            delivery = (
                await session.execute(
                    select(ReportDelivery).where(
                        ReportDelivery.report_id == report_id,
                        ReportDelivery.channel == channel_value,
                        ReportDelivery.destination_fingerprint == destination,
                    )
                )
            ).scalar_one()
            return ClaimResult(delivery.id, result.rowcount == 1, delivery.status)

    async def mark_report_delivery(
        self,
        *,
        delivery_id: int,
        status: str,
        safe_error_type: str | None = None,
        sent_at: datetime | None = None,
    ) -> ReportDelivery:
        status_value = _identifier(status, "status", max_length=16)
        if status_value not in {"SENT", "FAILED", "UNKNOWN"}:
            raise RepositoryValidationError("unsupported report delivery status")
        async with self.database.transaction() as session:
            delivery = await session.get(ReportDelivery, delivery_id)
            if delivery is None:
                raise RepositoryValidationError("report delivery does not exist")
            delivery.status = status_value
            delivery.attempt_count += 1
            delivery.safe_error_type = _safe_error_type(safe_error_type)
            delivery.sent_at = (
                _aware_utc(sent_at or utc_now(), "sent_at") if status_value == "SENT" else None
            )
            await session.flush()
            return delivery

    async def claim_snapshot(
        self,
        *,
        idempotency_key: str,
        captured_at: datetime,
        data_as_of: datetime,
        base_currency: str = "KRW",
    ) -> ClaimResult:
        key = idempotency_key.strip()
        if not key or len(key) > 160:
            raise RepositoryValidationError("idempotency_key is invalid")
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            statement = (
                sqlite_insert(PortfolioSnapshot)
                .values(
                    portfolio_id=portfolio.id,
                    idempotency_key=key,
                    captured_at=_aware_utc(captured_at, "captured_at"),
                    data_as_of=_aware_utc(data_as_of, "data_as_of"),
                    status="COLLECTING",
                    base_currency=_currency(base_currency),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            result = await session.execute(statement)
            snapshot = (
                await session.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.idempotency_key == key)
                )
            ).scalar_one()
            created = result.rowcount == 1
            if not created and snapshot.status == "COLLECTING":
                # A previous process may have stopped after writing only part of
                # the child rows. A resumed collection replaces that incomplete
                # generation so removed positions/cash cannot survive as ghosts.
                await session.execute(
                    delete(PositionSnapshot).where(PositionSnapshot.snapshot_id == snapshot.id)
                )
                await session.execute(
                    delete(CashSnapshot).where(CashSnapshot.snapshot_id == snapshot.id)
                )
                await session.execute(
                    delete(SnapshotSource).where(SnapshotSource.snapshot_id == snapshot.id)
                )
                snapshot.captured_at = _aware_utc(captured_at, "captured_at")
                snapshot.data_as_of = _aware_utc(data_as_of, "data_as_of")
                snapshot.base_currency = _currency(base_currency)
                snapshot.total_market_value = None
                snapshot.total_cost_basis = None
                snapshot.total_unrealized_profit = None
                snapshot.total_return_pct = None
                snapshot.finalized_at = None
                await session.flush()
            return ClaimResult(snapshot.id, created, snapshot.status)

    async def record_snapshot_source(
        self,
        *,
        snapshot_id: int,
        source_key: str,
        status: str,
        account_id: int | None = None,
        observed_at: datetime | None = None,
        safe_error_type: str | None = None,
        carried_from_snapshot_id: int | None = None,
    ) -> SnapshotSource:
        source = _identifier(source_key, "source_key", max_length=80)
        status_value = _identifier(status, "status", max_length=16)
        if status_value not in {
            "PENDING",
            "SUCCESS",
            "PARTIAL",
            "FAILED",
            "STALE",
            "UNSUPPORTED",
        }:
            raise RepositoryValidationError("unsupported snapshot source status")
        error_type = _safe_error_type(safe_error_type)
        if status_value == "FAILED" and error_type is None:
            raise RepositoryValidationError("failed sources require safe_error_type")
        async with self.database.transaction() as session:
            statement = sqlite_insert(SnapshotSource).values(
                snapshot_id=snapshot_id,
                source_key=source,
                account_id=account_id,
                status=status_value,
                observed_at=(
                    None if observed_at is None else _aware_utc(observed_at, "observed_at")
                ),
                safe_error_type=error_type,
                carried_from_snapshot_id=carried_from_snapshot_id,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["snapshot_id", "source_key"],
                    set_={
                        "account_id": account_id,
                        "status": status_value,
                        "observed_at": (
                            None if observed_at is None else _aware_utc(observed_at, "observed_at")
                        ),
                        "safe_error_type": error_type,
                        "carried_from_snapshot_id": carried_from_snapshot_id,
                    },
                )
            )
            return (
                await session.execute(
                    select(SnapshotSource).where(
                        SnapshotSource.snapshot_id == snapshot_id,
                        SnapshotSource.source_key == source,
                    )
                )
            ).scalar_one()

    async def add_position(
        self,
        *,
        snapshot_id: int,
        account_id: int,
        market: str,
        ticker: str,
        currency: str,
        quantity: Decimal | int | str | float,
        average_price: Decimal | int | str | float | None = None,
        current_price: Decimal | int | str | float | None = None,
        cost_basis: Decimal | int | str | float | None = None,
        market_value: Decimal | int | str | float | None = None,
        unrealized_profit: Decimal | int | str | float | None = None,
        return_pct: Decimal | int | str | float | None = None,
        price_as_of: datetime | None = None,
        fx_rate_id: int | None = None,
        value_in_base: Decimal | int | str | float | None = None,
        company_name: str | None = None,
        asset_type: str = "EQUITY",
    ) -> PositionSnapshot:
        currency_value = _currency(currency)
        async with self.database.transaction() as session:
            instrument = await self._instrument(
                session,
                market=market,
                ticker=ticker,
                currency=currency_value,
                asset_type=asset_type,
                company_name=company_name,
            )
            values = {
                "snapshot_id": snapshot_id,
                "account_id": account_id,
                "instrument_id": instrument.id,
                "currency": currency_value,
                "quantity": _decimal(quantity, "quantity", minimum=Decimal("0")),
                "average_price": None
                if average_price is None
                else _decimal(average_price, "average_price", minimum=Decimal("0")),
                "current_price": None
                if current_price is None
                else _decimal(current_price, "current_price", minimum=Decimal("0")),
                "cost_basis": None if cost_basis is None else _decimal(cost_basis, "cost_basis"),
                "market_value": None
                if market_value is None
                else _decimal(market_value, "market_value"),
                "unrealized_profit": None
                if unrealized_profit is None
                else _decimal(unrealized_profit, "unrealized_profit"),
                "return_pct": None if return_pct is None else _decimal(return_pct, "return_pct"),
                "price_as_of": None
                if price_as_of is None
                else _aware_utc(price_as_of, "price_as_of"),
                "fx_rate_id": fx_rate_id,
                "value_in_base": None
                if value_in_base is None
                else _decimal(value_in_base, "value_in_base"),
            }
            statement = sqlite_insert(PositionSnapshot).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["snapshot_id", "account_id", "instrument_id"],
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in values
                        if key not in {"snapshot_id", "account_id", "instrument_id"}
                    },
                )
            )
            return (
                await session.execute(
                    select(PositionSnapshot).where(
                        PositionSnapshot.snapshot_id == snapshot_id,
                        PositionSnapshot.account_id == account_id,
                        PositionSnapshot.instrument_id == instrument.id,
                    )
                )
            ).scalar_one()

    async def add_cash(
        self,
        *,
        snapshot_id: int,
        account_id: int,
        currency: str,
        amount: Decimal | int | str | float,
        fx_rate_id: int | None = None,
        value_in_base: Decimal | int | str | float | None = None,
    ) -> CashSnapshot:
        values = {
            "snapshot_id": snapshot_id,
            "account_id": account_id,
            "currency": _currency(currency),
            "amount": _decimal(amount, "amount"),
            "fx_rate_id": fx_rate_id,
            "value_in_base": None
            if value_in_base is None
            else _decimal(value_in_base, "value_in_base"),
        }
        async with self.database.transaction() as session:
            statement = sqlite_insert(CashSnapshot).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["snapshot_id", "account_id", "currency"],
                    set_={
                        "amount": statement.excluded.amount,
                        "fx_rate_id": statement.excluded.fx_rate_id,
                        "value_in_base": statement.excluded.value_in_base,
                    },
                )
            )
            return (
                await session.execute(
                    select(CashSnapshot).where(
                        CashSnapshot.snapshot_id == snapshot_id,
                        CashSnapshot.account_id == account_id,
                        CashSnapshot.currency == values["currency"],
                    )
                )
            ).scalar_one()

    async def finalize_snapshot(
        self,
        *,
        snapshot_id: int,
        status: str,
        total_market_value: Decimal | int | str | float | None = None,
        total_cost_basis: Decimal | int | str | float | None = None,
        total_unrealized_profit: Decimal | int | str | float | None = None,
        total_return_pct: Decimal | int | str | float | None = None,
        finalized_at: datetime | None = None,
    ) -> PortfolioSnapshot:
        status_value = _identifier(status, "status", max_length=16)
        if status_value not in {"COMPLETE", "PARTIAL", "FAILED"}:
            raise RepositoryValidationError("unsupported final snapshot status")
        async with self.database.transaction() as session:
            snapshot = await session.get(PortfolioSnapshot, snapshot_id)
            if snapshot is None:
                raise RepositoryValidationError("snapshot does not exist")
            sources = list(
                (
                    await session.execute(
                        select(SnapshotSource).where(SnapshotSource.snapshot_id == snapshot_id)
                    )
                ).scalars()
            )
            if status_value == "COMPLETE" and (
                not sources or any(source.status != "SUCCESS" for source in sources)
            ):
                raise RepositoryValidationError(
                    "COMPLETE snapshots require every declared source to succeed"
                )
            snapshot.status = status_value
            snapshot.total_market_value = (
                None
                if total_market_value is None
                else _decimal(total_market_value, "total_market_value")
            )
            snapshot.total_cost_basis = (
                None if total_cost_basis is None else _decimal(total_cost_basis, "total_cost_basis")
            )
            snapshot.total_unrealized_profit = (
                None
                if total_unrealized_profit is None
                else _decimal(total_unrealized_profit, "total_unrealized_profit")
            )
            snapshot.total_return_pct = (
                None if total_return_pct is None else _decimal(total_return_pct, "total_return_pct")
            )
            snapshot.finalized_at = _aware_utc(finalized_at or utc_now(), "finalized_at")
            await session.flush()
            return snapshot

    async def record_transaction_once(
        self,
        *,
        account_id: int,
        source_key: str,
        transaction_type: str,
        status: str,
        traded_at: datetime,
        currency: str,
        market: str | None = None,
        ticker: str | None = None,
        asset_type: str = "EQUITY",
        quantity: Decimal | int | str | float = Decimal("0"),
        unit_price: Decimal | int | str | float | None = None,
        gross_amount: Decimal | int | str | float | None = None,
        fee: Decimal | int | str | float | None = None,
        tax: Decimal | int | str | float | None = None,
        net_amount: Decimal | int | str | float | None = None,
        settled_at: datetime | None = None,
    ) -> tuple[Transaction, bool]:
        key = source_key.strip()
        if not key or len(key) > 160:
            raise RepositoryValidationError("source_key is invalid")
        currency_value = _currency(currency)
        async with self.database.transaction() as session:
            instrument_id: int | None = None
            if market is not None or ticker is not None:
                if not market or not ticker:
                    raise RepositoryValidationError("market and ticker must be provided together")
                instrument = await self._instrument(
                    session,
                    market=market,
                    ticker=ticker,
                    currency=currency_value,
                    asset_type=asset_type,
                )
                instrument_id = instrument.id
            values = {
                "account_id": account_id,
                "source_key": key,
                "instrument_id": instrument_id,
                "transaction_type": _identifier(
                    transaction_type, "transaction_type", max_length=24
                ),
                "status": _identifier(status, "status", max_length=24),
                "traded_at": _aware_utc(traded_at, "traded_at"),
                "settled_at": None if settled_at is None else _aware_utc(settled_at, "settled_at"),
                "currency": currency_value,
                "quantity": _decimal(quantity, "quantity", minimum=Decimal("0")),
                "unit_price": None
                if unit_price is None
                else _decimal(unit_price, "unit_price", minimum=Decimal("0")),
                "gross_amount": None
                if gross_amount is None
                else _decimal(gross_amount, "gross_amount"),
                "fee": None if fee is None else _decimal(fee, "fee"),
                "tax": None if tax is None else _decimal(tax, "tax"),
                "net_amount": None if net_amount is None else _decimal(net_amount, "net_amount"),
            }
            result = await session.execute(
                sqlite_insert(Transaction)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["account_id", "source_key"])
            )
            transaction = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.account_id == account_id,
                        Transaction.source_key == key,
                    )
                )
            ).scalar_one()
            return transaction, result.rowcount == 1

    async def upsert_target_allocation(
        self,
        *,
        scope_type: str,
        scope_key: str,
        target_weight: Decimal | int | str | float,
        effective_from: datetime | None = None,
    ) -> TargetAllocation:
        scope_type_value = _identifier(scope_type, "scope_type", max_length=24)
        scope_key_value = scope_key.strip().upper()
        if not scope_key_value or len(scope_key_value) > 120:
            raise RepositoryValidationError("scope_key is invalid")
        weight = _decimal(
            target_weight,
            "target_weight",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        effective = _aware_utc(effective_from or utc_now(), "effective_from")
        async with self.database.transaction() as session:
            portfolio = await self._portfolio(session)
            statement = sqlite_insert(TargetAllocation).values(
                portfolio_id=portfolio.id,
                scope_type=scope_type_value,
                scope_key=scope_key_value,
                target_weight=weight,
                effective_from=effective,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["portfolio_id", "scope_type", "scope_key"],
                    set_={
                        "target_weight": weight,
                        "effective_from": effective,
                        "updated_at": utc_now(),
                    },
                )
            )
            return (
                await session.execute(
                    select(TargetAllocation).where(
                        TargetAllocation.portfolio_id == portfolio.id,
                        TargetAllocation.scope_type == scope_type_value,
                        TargetAllocation.scope_key == scope_key_value,
                    )
                )
            ).scalar_one()


SqlAlchemyRepository = SQLitePortfolioRepository
SQLAlchemyRepository = SQLitePortfolioRepository
