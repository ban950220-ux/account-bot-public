"""Concurrent broker collection with explicit partial-result semantics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from account_bot.brokers import ReadOnlyBroker
from account_bot.brokers.helpers import failure_result
from account_bot.domain import (
    Account,
    BrokerResult,
    CashBalance,
    Currency,
    FxQuote,
    Position,
    Transaction,
)
from account_bot.market import MarketDataProvider
from account_bot.portfolio import PortfolioAggregator, PortfolioSnapshot


class FxRateStore(Protocol):
    async def save_fx_rate(
        self,
        *,
        base: str,
        quote: str,
        rate: Decimal,
        observed_at: datetime,
        provider: str,
    ) -> None: ...

    async def get_last_fx_rate(
        self,
        *,
        base: str,
        quote: str,
        not_older_than: datetime,
    ) -> tuple[Decimal, datetime, str] | None: ...


@dataclass(frozen=True, slots=True)
class PortfolioRefresh:
    snapshot: PortfolioSnapshot
    account_results: tuple[BrokerResult[Account], ...]
    position_results: tuple[BrokerResult[Position], ...]
    cash_results: tuple[BrokerResult[CashBalance], ...]
    fx_quote: FxQuote | None
    collected_at: datetime


class AccountBotService:
    def __init__(
        self,
        brokers: list[ReadOnlyBroker] | tuple[ReadOnlyBroker, ...],
        *,
        market_provider: MarketDataProvider | None = None,
        fx_store: FxRateStore | None = None,
        broker_timeout_seconds: float = 20.0,
        maximum_stale_fx: timedelta = timedelta(days=3),
    ) -> None:
        names = [broker.name for broker in brokers]
        if len(names) != len(set(names)):
            raise ValueError("broker names must be unique")
        if broker_timeout_seconds <= 0:
            raise ValueError("broker timeout must be positive")
        self.brokers = tuple(brokers)
        self.market_provider = market_provider
        self.fx_store = fx_store
        self.broker_timeout_seconds = broker_timeout_seconds
        self.maximum_stale_fx = maximum_stale_fx
        self._last_in_memory_fx: FxQuote | None = None
        self._refresh_lock = asyncio.Lock()

    async def _call(self, broker: ReadOnlyBroker, awaitable):
        try:
            return await asyncio.wait_for(awaitable, timeout=self.broker_timeout_seconds)
        except Exception as exc:
            return failure_result(broker.name, exc)

    async def _load_fx(self, needs_usd: bool) -> FxQuote | None:
        if not needs_usd:
            return None
        now = datetime.now(timezone.utc)
        if self.market_provider is not None:
            try:
                quote = await self.market_provider.get_fx("USD", "KRW")
                domain_quote = FxQuote(
                    Currency.USD,
                    Currency.KRW,
                    quote.rate,
                    observed_at=quote.as_of,
                    is_stale=quote.stale,
                )
                self._last_in_memory_fx = domain_quote
                if self.fx_store is not None:
                    try:
                        await self.fx_store.save_fx_rate(
                            base="USD",
                            quote="KRW",
                            rate=quote.rate,
                            observed_at=quote.as_of,
                            provider=quote.provider,
                        )
                    except Exception:
                        # Persistence is best-effort; a fresh provider quote is
                        # still valid for this coherent in-memory refresh.
                        pass
                return domain_quote
            except Exception:
                # Fall through to last-good values; no provider body or token is
                # ever copied into the result.
                pass

        fallback_quotes: list[FxQuote] = []
        if self.fx_store is not None:
            try:
                stored = await self.fx_store.get_last_fx_rate(
                    base="USD",
                    quote="KRW",
                    not_older_than=now - self.maximum_stale_fx,
                )
                if stored is not None:
                    rate, observed_at, _provider = stored
                    fallback_quotes.append(
                        FxQuote(
                            Currency.USD,
                            Currency.KRW,
                            rate,
                            observed_at=observed_at,
                            is_stale=True,
                        )
                    )
            except Exception:
                pass

        memory = self._last_in_memory_fx
        if (
            memory is not None
            and memory.observed_at is not None
            and memory.observed_at >= now - self.maximum_stale_fx
        ):
            fallback_quotes.append(
                FxQuote(
                    memory.base,
                    memory.quote,
                    memory.rate,
                    observed_at=memory.observed_at,
                    is_stale=True,
                )
            )
        if not fallback_quotes:
            return None
        # A best-effort database write may have failed after a fresh provider
        # quote was accepted.  Prefer the newest known observation across the
        # persistent and in-process last-good sources instead of regressing to
        # an older database value on the next provider outage.
        return max(fallback_quotes, key=lambda quote: quote.observed_at)

    async def refresh_portfolio(
        self,
        *,
        broker_names: set[str] | frozenset[str] | None = None,
    ) -> PortfolioRefresh:
        """Collect the requested brokers and never substitute a failure with zero."""

        async with self._refresh_lock:
            selected = (
                self.brokers
                if broker_names is None
                else tuple(broker for broker in self.brokers if broker.name in broker_names)
            )
            if not selected:
                raise ValueError("no matching broker source was selected")
            account_results = tuple(
                await asyncio.gather(
                    *(self._call(broker, broker.get_accounts()) for broker in selected)
                )
            )
            # Accounts are loaded first so an adapter such as Toss can reuse its
            # safe opaque account sequence for the two parallel queries below.
            position_results, cash_results = await asyncio.gather(
                asyncio.gather(
                    *(self._call(broker, broker.get_positions()) for broker in selected)
                ),
                asyncio.gather(*(self._call(broker, broker.get_cash()) for broker in selected)),
            )
            position_tuple = tuple(position_results)
            cash_tuple = tuple(cash_results)
            needs_usd = any(
                item.instrument.currency == Currency.USD
                for result in position_tuple
                for item in result.items
            ) or any(
                item.currency == Currency.USD for result in cash_tuple for item in result.items
            )
            fx_quote = await self._load_fx(needs_usd)
            quotes = (fx_quote,) if fx_quote is not None else ()
            snapshot = PortfolioAggregator(Currency.KRW, quotes).aggregate(
                position_tuple,
                expected_brokers=(broker.name for broker in selected),
                cash_results=cash_tuple,
            )
            return PortfolioRefresh(
                snapshot=snapshot,
                account_results=account_results,
                position_results=position_tuple,
                cash_results=cash_tuple,
                fx_quote=fx_quote,
                collected_at=datetime.now(timezone.utc),
            )

    async def get_transactions(
        self,
        *,
        start_date: date,
        end_date: date,
        broker_names: set[str] | frozenset[str] | None = None,
    ) -> tuple[BrokerResult[Transaction], ...]:
        selected = (
            self.brokers
            if broker_names is None
            else tuple(broker for broker in self.brokers if broker.name in broker_names)
        )
        return tuple(
            await asyncio.gather(
                *(
                    self._call(
                        broker,
                        broker.get_transactions(start_date=start_date, end_date=end_date),
                    )
                    for broker in selected
                )
            )
        )
