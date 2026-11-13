"""Translate persistence records into service/domain contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from account_bot.domain import AssetType, Currency, InstrumentKey, Money
from account_bot.portfolio import RecurringBuyPlan
from account_bot.storage import RecurringBuyView, SQLitePortfolioRepository


def _currency(value: str) -> Currency:
    return Currency(value.upper())


LOGGER = logging.getLogger(__name__)


def _scheduled_weekdays(memo: str | None) -> tuple[int, ...]:
    text = memo or ""
    if "[schedule=MON]" in text:
        return (0,)
    if "[schedule=FRI]" in text:
        return (4,)
    return (0, 1, 2, 3, 4)


class SqlAlchemyRecurringPlanGateway:
    """Domain-facing recurring/thesis gateway backed by append-only records."""

    def __init__(
        self,
        repository: SQLitePortfolioRepository,
        *,
        timezone_name: str = "Asia/Seoul",
    ) -> None:
        self.repository = repository
        self.timezone_name = timezone_name

    async def _to_domain(self, view: RecurringBuyView) -> RecurringBuyPlan:
        thesis = await self.repository.get_current_investment_thesis(
            market=view.market,
            ticker=view.ticker,
        )
        instrument_currency = _currency(view.instrument_currency)
        amount_currency = _currency(view.plan.currency)
        target = (
            None if view.plan.target_weight is None else view.plan.target_weight * Decimal("100")
        )
        started_at = view.plan.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return RecurringBuyPlan(
            instrument=InstrumentKey(
                view.market,
                view.ticker,
                AssetType(view.asset_type),
                currency=instrument_currency,
            ),
            daily_amount=Money(view.plan.daily_amount, amount_currency),
            enabled=view.plan.enabled,
            target_weight_pct=target,
            investment_thesis=thesis.thesis_text if thesis is not None else "",
            memo=view.plan.memo or "",
            scheduled_weekdays=_scheduled_weekdays(view.plan.memo),
            started_at=started_at.astimezone(timezone.utc).date(),
        )

    async def list_plans(self, *, enabled_only: bool = False) -> tuple[RecurringBuyPlan, ...]:
        views = await self.repository.list_recurring_buy_views(enabled_only=enabled_only)
        return tuple(await asyncio.gather(*(self._to_domain(view) for view in views)))

    async def upsert_plan(
        self,
        *,
        market: str,
        ticker: str,
        amount: Decimal,
        currency: Currency,
        target_weight_pct: Decimal | None,
        memo: str = "",
    ) -> RecurringBuyPlan:
        plan_key = f"manual:{market.upper()}:{ticker.upper()}"
        existing = await self.repository.get_recurring_buy_view(plan_key)
        await self.repository.upsert_recurring_buy(
            plan_key=plan_key,
            market=market,
            ticker=ticker,
            daily_amount=amount,
            currency=currency.value,
            target_weight=(
                None if target_weight_pct is None else target_weight_pct / Decimal("100")
            ),
            enabled=True,
            source="TELEGRAM",
            timezone_name=self.timezone_name,
            started_at=existing.plan.started_at if existing is not None else None,
            memo=memo,
        )
        view = await self.repository.get_recurring_buy_view(plan_key)
        if view is None:
            raise RuntimeError("recurring plan was not persisted")
        return await self._to_domain(view)

    async def set_thesis(self, *, market: str, ticker: str, thesis: str) -> None:
        currency = "KRW" if market.upper() in {"KOSPI", "KOSDAQ", "KRX", "NXT"} else "USD"
        content_hash = hashlib.sha256(thesis.encode("utf-8")).hexdigest()
        await self.repository.set_investment_thesis(
            revision_key=f"telegram:{market.upper()}:{ticker.upper()}:{content_hash[:32]}",
            market=market,
            ticker=ticker,
            currency=currency,
            thesis_text=thesis,
        )

    async def apply_group_change(
        self,
        *,
        asset_type: AssetType,
        delta: Decimal | None,
        stop: bool,
        excluded: frozenset[str],
    ) -> tuple[RecurringBuyPlan, ...]:
        changed_keys: set[str] = set()
        views = await self.repository.list_recurring_buy_views(enabled_only=True)
        for view in views:
            ticker = view.ticker.upper()
            if AssetType(view.asset_type) != asset_type or ticker in excluded:
                continue
            plan = view.plan
            if stop:
                await self.repository.set_recurring_buy_enabled(
                    plan_key=plan.plan_key, enabled=False
                )
            else:
                assert delta is not None
                minimum = Decimal("2000") if plan.currency == "KRW" else Decimal("1")
                amount = max(minimum, plan.daily_amount + delta)
                if amount == plan.daily_amount:
                    continue
                await self.repository.upsert_recurring_buy(
                    plan_key=plan.plan_key,
                    market=view.market,
                    ticker=view.ticker,
                    daily_amount=amount,
                    currency=plan.currency,
                    instrument_currency=view.instrument_currency,
                    asset_type=view.asset_type,
                    target_weight=plan.target_weight,
                    enabled=plan.enabled,
                    account_id=plan.account_id,
                    source=plan.source,
                    timezone_name=plan.timezone_name,
                    started_at=plan.started_at,
                    memo=plan.memo,
                )
            changed_keys.add(plan.plan_key)
        updated = await self.repository.list_recurring_buy_views(enabled_only=False)
        return tuple(
            await asyncio.gather(
                *(self._to_domain(view) for view in updated if view.plan.plan_key in changed_keys)
            )
        )


class SnapshotAugmentedRecurringPlanGateway:
    """Merge safe, read-only recurring plans with locally stored plans."""

    def __init__(self, delegate: SqlAlchemyRecurringPlanGateway, path: Path | None) -> None:
        self.delegate = delegate
        self.path = path / "recurring.json" if path is not None and path.is_dir() else path

    def _snapshot_plans(self) -> tuple[RecurringBuyPlan, ...]:
        if self.path is None or not self.path.is_file():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or set(payload) - {"as_of", "plans"}:
                raise ValueError("recurring snapshot root is invalid")
            rows = payload.get("plans")
            if not isinstance(rows, list):
                raise ValueError("recurring snapshot plans must be a list")
            plans: list[RecurringBuyPlan] = []
            allowed = {
                "market",
                "ticker",
                "asset_type",
                "instrument_currency",
                "daily_amount",
                "amount_currency",
                "broker",
                "started_at",
                "memo",
                "enabled",
            }
            for row in rows:
                if not isinstance(row, dict) or set(row) - allowed:
                    raise ValueError("recurring snapshot row is invalid")
                broker = str(row.get("broker") or "meritz").strip().lower()
                if not re.fullmatch(r"[a-z][a-z0-9_-]{1,23}", broker):
                    raise ValueError("recurring snapshot broker alias is invalid")
                started = row.get("started_at")
                plans.append(
                    RecurringBuyPlan(
                        instrument=InstrumentKey(
                            str(row["market"]),
                            str(row["ticker"]),
                            AssetType(str(row.get("asset_type") or "EQUITY").upper()),
                            Currency(str(row.get("instrument_currency") or "USD").upper()),
                        ),
                        daily_amount=Money(
                            Decimal(str(row["daily_amount"])),
                            Currency(str(row.get("amount_currency") or "KRW").upper()),
                        ),
                        broker=broker,
                        enabled=bool(row.get("enabled", True)),
                        started_at=date.fromisoformat(str(started)) if started else None,
                        memo=str(row.get("memo") or ""),
                    )
                )
            return tuple(plans)
        except Exception as exc:
            LOGGER.warning(
                "recurring snapshot ignored error_type=%s", type(exc).__name__
            )
            return ()

    async def list_plans(self, *, enabled_only: bool = False) -> tuple[RecurringBuyPlan, ...]:
        local = await self.delegate.list_plans(enabled_only=enabled_only)
        snapshot = await asyncio.to_thread(self._snapshot_plans)
        if enabled_only:
            snapshot = tuple(plan for plan in snapshot if plan.enabled)
        merged: dict[tuple[str, str | None], RecurringBuyPlan] = {
            (plan.instrument.stable_id, plan.broker): plan for plan in snapshot
        }
        for plan in local:
            # A Telegram-created plan targets the configured Kiwoom accumulation
            # source by default; it may still be overridden in future storage UI.
            broker = (
                "candidate"
                if "[source=USER_CANDIDATE]" in plan.memo
                else (plan.broker or "kiwoom_accum")
            )
            targeted = replace(plan, broker=broker)
            merged[(targeted.instrument.stable_id, targeted.broker)] = targeted
        return tuple(
            sorted(
                merged.values(),
                key=lambda plan: (plan.broker or "", plan.instrument.stable_id),
            )
        )

    async def upsert_plan(self, **kwargs):
        plan = await self.delegate.upsert_plan(**kwargs)
        return replace(plan, broker=plan.broker or "kiwoom_accum")

    async def set_thesis(self, **kwargs) -> None:
        await self.delegate.set_thesis(**kwargs)

    def _change_snapshot_group(
        self,
        *,
        asset_type: AssetType,
        delta: Decimal | None,
        stop: bool,
        excluded: frozenset[str],
    ) -> set[tuple[str, str]]:
        if self.path is None or not self.path.is_file():
            return set()
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        changed: set[tuple[str, str]] = set()
        for row in payload.get("plans", []):
            ticker = str(row.get("ticker", "")).upper()
            row_type = AssetType(str(row.get("asset_type") or "EQUITY").upper())
            if row_type != asset_type or ticker in excluded or not row.get("enabled", True):
                continue
            if stop:
                row["enabled"] = False
            else:
                assert delta is not None
                currency = Currency(str(row.get("amount_currency") or "KRW").upper())
                minimum = Decimal("1000") if currency == Currency.KRW else Decimal("1")
                current = Decimal(str(row["daily_amount"]))
                amount = max(minimum, current + delta)
                if amount == current:
                    continue
                row["daily_amount"] = format(amount, "f")
            changed.add((str(row.get("market", "US")).upper(), ticker))
        if changed:
            payload["as_of"] = datetime.now(timezone.utc).isoformat()
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(self.path)
        return changed

    async def apply_group_change(self, **kwargs) -> tuple[RecurringBuyPlan, ...]:
        local = await self.delegate.apply_group_change(**kwargs)
        snapshot_keys = await asyncio.to_thread(self._change_snapshot_group, **kwargs)
        snapshot = await asyncio.to_thread(self._snapshot_plans)
        changed_snapshot = tuple(
            plan
            for plan in snapshot
            if (plan.instrument.market, plan.instrument.ticker) in snapshot_keys
        )
        targeted_local = tuple(
            replace(
                plan,
                broker=(
                    "candidate"
                    if "[source=USER_CANDIDATE]" in plan.memo
                    else (plan.broker or "kiwoom_accum")
                ),
            )
            for plan in local
        )
        return targeted_local + changed_snapshot


class SqlAlchemyFxRateStore:
    """Last-good FX storage used by :class:`AccountBotService`."""

    def __init__(self, repository: SQLitePortfolioRepository) -> None:
        self.repository = repository

    async def save_fx_rate(
        self,
        *,
        base: str,
        quote: str,
        rate: Decimal,
        observed_at: datetime,
        provider: str,
    ) -> None:
        canonical = f"{provider}|{base}|{quote}|{observed_at.isoformat()}|{rate}"
        observation_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        await self.repository.add_fx_rate(
            observation_key=observation_key,
            provider=provider,
            base_currency=base,
            quote_currency=quote,
            rate=rate,
            effective_at=observed_at,
        )

    async def get_last_fx_rate(
        self,
        *,
        base: str,
        quote: str,
        not_older_than: datetime,
    ) -> tuple[Decimal, datetime, str] | None:
        now = datetime.now(timezone.utc)
        lower = not_older_than.astimezone(timezone.utc)
        max_age = now - lower
        if max_age.total_seconds() <= 0:
            return None
        value = await self.repository.get_latest_fx_rate(
            base_currency=base,
            quote_currency=quote,
            max_age=max_age,
            now=now,
        )
        if value is None:
            return None
        return value.rate, value.effective_at, value.provider
