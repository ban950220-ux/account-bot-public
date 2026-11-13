"""Deterministic cross-broker aggregation with explicit incompleteness."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from account_bot.domain import (
    US_MARKET_ALIASES,
    BrokerResult,
    BrokerStatus,
    CashBalance,
    Currency,
    FxQuote,
    InstrumentKey,
    Money,
    Position,
)

from .models import (
    AggregatedPosition,
    BrokerQuantity,
    ConvertedCashBalance,
    PortfolioSnapshot,
)


class _FxBook:
    def __init__(self, quotes: Iterable[FxQuote]) -> None:
        self._quotes: Dict[Tuple[Currency, Currency], FxQuote] = {}
        for quote in quotes:
            pair = (quote.base, quote.quote)
            if pair in self._quotes:
                raise ValueError(f"duplicate FX quote for {quote.base.value}/{quote.quote.value}")
            self._quotes[pair] = quote

    def convert(
        self, amount: Decimal, source: Currency, target: Currency
    ) -> Tuple[Optional[Decimal], bool]:
        if source == target:
            return amount, False
        direct = self._quotes.get((source, target))
        if direct is not None:
            return amount * direct.rate, direct.is_stale
        inverse = self._quotes.get((target, source))
        if inverse is not None:
            return amount / inverse.rate, inverse.is_stale
        return None, False


class PortfolioAggregator:
    """Aggregate read-only broker positions and optional cash balances."""

    def __init__(
        self,
        base_currency: Currency = Currency.KRW,
        fx_quotes: Iterable[FxQuote] = (),
    ) -> None:
        self.base_currency = Currency(base_currency)
        self._fx = _FxBook(fx_quotes)

    @staticmethod
    def _result_map(results: Sequence[BrokerResult[object]]) -> Mapping[str, BrokerResult[object]]:
        mapped: Dict[str, BrokerResult[object]] = {}
        for result in results:
            if result.broker in mapped:
                raise ValueError(f"duplicate broker result: {result.broker}")
            mapped[result.broker] = result
        return mapped

    def aggregate(
        self,
        position_results: Iterable[BrokerResult[Position]],
        expected_brokers: Optional[Iterable[str]] = None,
        cash_results: Iterable[BrokerResult[CashBalance]] = (),
    ) -> PortfolioSnapshot:
        position_results_tuple = tuple(position_results)
        cash_results_tuple = tuple(cash_results)
        position_map = self._result_map(position_results_tuple)  # type: ignore[arg-type]
        cash_map = self._result_map(cash_results_tuple)  # type: ignore[arg-type]
        actual_brokers = set(position_map) | set(cash_map)
        if expected_brokers is None:
            expected = actual_brokers
        else:
            expected = {str(value).strip().lower() for value in expected_brokers}
            if "" in expected:
                raise ValueError("expected broker name must not be empty")
            # Never silently discard a result merely because configuration lagged it.
            expected |= actual_brokers
        expected_tuple = tuple(sorted(expected))
        includes_cash = bool(cash_results_tuple)

        warnings = []
        incomplete_brokers = set()
        included_brokers = set()

        for broker in expected_tuple:
            result = position_map.get(broker)
            if result is None:
                incomplete_brokers.add(broker)
                warnings.append(f"{broker}:position_result_missing")
                continue
            if result.status != BrokerStatus.OK:
                incomplete_brokers.add(broker)
                warnings.append(f"{broker}:positions_{result.status.value}")
            if result.status in {
                BrokerStatus.OK,
                BrokerStatus.PARTIAL,
                BrokerStatus.STALE,
            }:
                included_brokers.add(broker)

        if includes_cash:
            for broker in expected_tuple:
                result = cash_map.get(broker)
                if result is None:
                    incomplete_brokers.add(broker)
                    warnings.append(f"{broker}:cash_result_missing")
                    continue
                if result.status != BrokerStatus.OK:
                    incomplete_brokers.add(broker)
                    warnings.append(f"{broker}:cash_{result.status.value}")
                if result.status in {
                    BrokerStatus.OK,
                    BrokerStatus.PARTIAL,
                    BrokerStatus.STALE,
                }:
                    included_brokers.add(broker)

        usable_positions: list[Position] = []
        for result in position_results_tuple:
            if result.status not in {
                BrokerStatus.OK,
                BrokerStatus.PARTIAL,
                BrokerStatus.STALE,
            }:
                continue
            for position in result.items:
                if position.broker != result.broker:
                    raise ValueError(
                        f"position broker {position.broker!r} does not match "
                        f"envelope {result.broker!r}"
                    )
                if position.quantity > 0:
                    usable_positions.append(position)

        # Toss currently exposes a country-level US market while Kiwoom may
        # expose the exact exchange. Attach a generic US lot to one
        # unambiguous specific exchange, but never merge two known exchanges.
        specific_us_markets: Dict[Tuple[str, object, Currency], set[str]] = defaultdict(set)
        for position in usable_positions:
            instrument = position.instrument
            if instrument.market in US_MARKET_ALIASES and instrument.market not in {"US", "USA"}:
                specific_us_markets[
                    (instrument.ticker, instrument.asset_type, instrument.currency)
                ].add(instrument.market)

        grouped: Dict[InstrumentKey, list[Position]] = defaultdict(list)
        for position in usable_positions:
            instrument = position.instrument
            if instrument.market in {"US", "USA"}:
                candidates = specific_us_markets.get(
                    (instrument.ticker, instrument.asset_type, instrument.currency),
                    set(),
                )
                if len(candidates) == 1:
                    instrument = InstrumentKey(
                        next(iter(candidates)),
                        instrument.ticker,
                        instrument.asset_type,
                        instrument.currency,
                    )
            grouped[instrument].append(position)

        valuation_complete = True
        all_costs_complete = True
        used_stale_fx = False
        aggregated_positions = []
        included_position_amount = Decimal("0")
        included_cost_amount = Decimal("0")

        for instrument in sorted(grouped, key=lambda value: value.stable_id):
            lots = grouped[instrument]
            quantity = sum((lot.quantity for lot in lots), Decimal("0"))
            local_costs = [lot.effective_cost_basis for lot in lots]
            local_values = [lot.effective_market_value for lot in lots]
            cost_basis = (
                sum((value for value in local_costs if value is not None), Decimal("0"))
                if all(value is not None for value in local_costs)
                else None
            )
            market_value = (
                sum((value for value in local_values if value is not None), Decimal("0"))
                if all(value is not None for value in local_values)
                else None
            )
            if cost_basis is None:
                all_costs_complete = False
            average_price = cost_basis / quantity if cost_basis is not None else None
            current_price = market_value / quantity if market_value is not None else None
            unrealized = (
                market_value - cost_basis
                if market_value is not None and cost_basis is not None
                else None
            )
            return_pct = (
                unrealized / cost_basis * Decimal("100")
                if unrealized is not None and cost_basis not in (None, Decimal("0"))
                else None
            )

            market_value_base: Optional[Decimal] = None
            if market_value is None:
                valuation_complete = False
                warnings.append(f"{instrument.stable_id}:market_value_missing")
            else:
                market_value_base, stale = self._fx.convert(
                    market_value, instrument.currency, self.base_currency
                )
                used_stale_fx = used_stale_fx or stale
                if market_value_base is None:
                    valuation_complete = False
                    warnings.append(
                        f"{instrument.stable_id}:fx_to_{self.base_currency.value}_missing"
                    )
                else:
                    included_position_amount += market_value_base

            cost_basis_base: Optional[Decimal] = None
            if cost_basis is not None:
                cost_basis_base, stale = self._fx.convert(
                    cost_basis, instrument.currency, self.base_currency
                )
                used_stale_fx = used_stale_fx or stale
                if cost_basis_base is None:
                    all_costs_complete = False
                else:
                    included_cost_amount += cost_basis_base

            quantities: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for lot in lots:
                quantities[lot.broker] += lot.quantity
            broker_quantities = tuple(
                BrokerQuantity(broker, quantities[broker]) for broker in sorted(quantities)
            )
            company_name = next((lot.company_name for lot in lots if lot.company_name), "")
            aggregated_positions.append(
                AggregatedPosition(
                    instrument=instrument,
                    company_name=company_name,
                    quantity=quantity,
                    average_price=average_price,
                    current_price=current_price,
                    cost_basis=cost_basis,
                    market_value=market_value,
                    unrealized_profit=unrealized,
                    return_pct=return_pct,
                    cost_basis_base=cost_basis_base,
                    market_value_base=market_value_base,
                    weight_pct=None,
                    broker_quantities=broker_quantities,
                )
            )

        converted_cash = []
        included_cash_amount = Decimal("0")
        if includes_cash:
            for result in cash_results_tuple:
                if result.status not in {
                    BrokerStatus.OK,
                    BrokerStatus.PARTIAL,
                    BrokerStatus.STALE,
                }:
                    continue
                for cash in result.items:
                    if cash.broker != result.broker:
                        raise ValueError(
                            f"cash broker {cash.broker!r} does not match envelope {result.broker!r}"
                        )
                    amount_base, stale = self._fx.convert(
                        cash.amount, cash.currency, self.base_currency
                    )
                    used_stale_fx = used_stale_fx or stale
                    if amount_base is None:
                        valuation_complete = False
                        warnings.append(
                            f"{cash.broker}:cash_fx_{cash.currency.value}_to_"
                            f"{self.base_currency.value}_missing"
                        )
                    else:
                        included_cash_amount += amount_base
                    converted_cash.append(
                        ConvertedCashBalance(
                            broker=cash.broker,
                            account_id=cash.account_id,
                            currency=cash.currency,
                            amount=cash.amount,
                            amount_base=amount_base,
                        )
                    )

        if used_stale_fx:
            warnings.append("fx_quote_stale")

        statuses_complete = bool(expected_tuple) and not incomplete_brokers
        is_complete = statuses_complete and valuation_complete
        included_total_amount = included_position_amount + included_cash_amount
        total_market_value = (
            Money(included_total_amount, self.base_currency) if is_complete else None
        )

        if is_complete and included_total_amount > 0:
            aggregated_positions = [
                replace(
                    position,
                    weight_pct=(
                        position.market_value_base / included_total_amount * Decimal("100")
                        if position.market_value_base is not None
                        else None
                    ),
                )
                for position in aggregated_positions
            ]
            cash_weight = included_cash_amount / included_total_amount * Decimal("100")
        else:
            cash_weight = None

        included_cost = (
            Money(included_cost_amount, self.base_currency) if all_costs_complete else None
        )
        total_cost = included_cost if is_complete else None
        position_statuses = tuple(
            sorted((broker, result.status) for broker, result in position_map.items())
        )
        cash_statuses = tuple(
            sorted((broker, result.status) for broker, result in cash_map.items())
        )
        return PortfolioSnapshot(
            base_currency=self.base_currency,
            positions=tuple(aggregated_positions),
            cash_balances=tuple(converted_cash),
            included_position_value=Money(included_position_amount, self.base_currency),
            included_cash_value=Money(included_cash_amount, self.base_currency),
            included_market_value=Money(included_total_amount, self.base_currency),
            total_market_value=total_market_value,
            included_cost_basis=included_cost,
            total_cost_basis=total_cost,
            cash_weight_pct=cash_weight,
            is_complete=is_complete,
            includes_cash=includes_cash,
            expected_brokers=expected_tuple,
            included_brokers=tuple(sorted(included_brokers)),
            incomplete_brokers=tuple(sorted(incomplete_brokers)),
            position_statuses=position_statuses,
            cash_statuses=cash_statuses,
            warnings=tuple(dict.fromkeys(warnings)),
        )
