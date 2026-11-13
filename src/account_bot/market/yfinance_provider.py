"""Optional public market-data provider backed by yfinance.

This provider is best-effort and never replaces broker-reported holdings or
executions. Yahoo data can be delayed or missing, so every method raises a
sanitized :class:`MarketDataError` instead of inventing values.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .base import (
    DistributionPoint,
    DistributionSeries,
    EarningsData,
    FundamentalData,
    FxQuote,
    MarketDataError,
    MarketDataProvider,
    NewsItem,
    PriceQuote,
    TotalReturnPoint,
    TotalReturnSeries,
    TrendData,
    ValuationData,
)

_NEW_YORK = ZoneInfo("America/New_York")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def yahoo_symbol(market: str, ticker: str) -> str:
    market_name = market.upper()
    symbol = ticker.upper()
    # Kiwoom prefixes domestic security codes with "A"; US tickers such as
    # AAPL must never have that leading character stripped.
    if (
        market_name in {"KOSPI", "KOSDAQ", "KRX", "NXT"}
        and symbol.startswith("A")
        and symbol[1:].isdigit()
    ):
        symbol = symbol[1:]
    if market_name == "KOSPI" and symbol.isdigit():
        return f"{symbol.zfill(6)}.KS"
    if market_name == "KOSDAQ" and symbol.isdigit():
        return f"{symbol.zfill(6)}.KQ"
    return symbol


def yahoo_fx_candidates(base: str, quote: str) -> tuple[tuple[str, bool], ...]:
    """Return Yahoo symbols and whether their quote must be inverted."""

    base, quote = base.upper(), quote.upper()
    if (base, quote) == ("USD", "KRW"):
        return (("KRW=X", False),)
    if (base, quote) == ("KRW", "USD"):
        return (("KRW=X", True),)
    return ((f"{base}{quote}=X", False),)


def _coverage(values: list[Decimal | None]) -> Decimal:
    if not values:
        return Decimal("0")
    present = sum(item is not None for item in values)
    return Decimal(present) / Decimal(len(values))


def _weekly_close(
    rows: list[tuple[date, Decimal]],
    *,
    now: datetime | None = None,
) -> tuple[Decimal, datetime]:
    """Select the last completed trading session in the latest closed Friday week."""

    local_now = (now or datetime.now(timezone.utc)).astimezone(_NEW_YORK)
    eligible = local_now.date()
    if local_now.time() < time(16, 15):
        eligible -= timedelta(days=1)
    target_friday = eligible - timedelta(days=(eligible.weekday() - 4) % 7)
    candidates = [(day, close) for day, close in rows if day <= target_friday and close > 0]
    if not candidates:
        raise MarketDataError("weekly close provider returned no completed session")
    session_day, close = max(candidates, key=lambda item: item[0])
    closed_at = datetime.combine(session_day, time(16), tzinfo=_NEW_YORK).astimezone(timezone.utc)
    return close, closed_at


def _statement_pair(frame: Any, names: tuple[str, ...]) -> tuple[Decimal | None, Decimal | None]:
    if frame is None or getattr(frame, "empty", True):
        return None, None
    for name in names:
        if name not in frame.index:
            continue
        row = frame.loc[name]
        values = [_decimal(value) for value in list(row)]
        present = [value for value in values if value is not None]
        return (
            present[0] if present else None,
            present[1] if len(present) > 1 else None,
        )
    return None, None


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    @staticmethod
    def _ticker(market: str, ticker: str):
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise MarketDataError("yfinance provider is not installed") from exc
        return yf.Ticker(yahoo_symbol(market, ticker))

    async def get_price(self, market: str, ticker: str) -> PriceQuote:
        symbol = yahoo_symbol(market, ticker)
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            try:
                async with httpx.AsyncClient(
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as client:
                    response = await client.get(
                        f"https://{host}/v8/finance/chart/{symbol}",
                        params={"range": "5d", "interval": "1d"},
                    )
                    response.raise_for_status()
                    result = response.json()["chart"]["result"][0]
                    meta = result["meta"]
                    price = _decimal(meta.get("regularMarketPrice"))
                    if price is None or price <= 0:
                        close = result.get("indicators", {}).get("quote", [{}])[0].get(
                            "close", []
                        )
                        price = next(
                            (
                                value
                                for raw in reversed(close)
                                if (value := _decimal(raw)) is not None and value > 0
                            ),
                            None,
                        )
                    if price is None or price <= 0:
                        continue
                    observed_raw = meta.get("regularMarketTime")
                    observed_at = (
                        datetime.fromtimestamp(int(observed_raw), tz=timezone.utc)
                        if observed_raw
                        else datetime.now(timezone.utc)
                    )
                    return PriceQuote(
                        market=market.upper(),
                        ticker=ticker.upper(),
                        currency=str(meta.get("currency") or "USD").upper(),
                        price=price,
                        as_of=observed_at,
                        provider="yahoo-chart",
                    )
            except Exception:
                continue

        def load() -> PriceQuote:
            obj = self._ticker(market, ticker)
            try:
                fast = obj.fast_info
                price = _decimal(fast.get("last_price"))
                currency = str(
                    fast.get("currency") or ("KRW" if market.upper().startswith("KOS") else "USD")
                )
            except Exception as exc:
                raise MarketDataError("price provider request failed") from exc
            if price is None or price <= 0:
                raise MarketDataError("price provider returned no usable price")
            return PriceQuote(
                market=market.upper(),
                ticker=ticker.upper(),
                currency=currency.upper(),
                price=price,
                as_of=datetime.now(timezone.utc),
                provider=self.name,
            )

        return await asyncio.to_thread(load)

    async def get_trend(self, market: str, ticker: str) -> TrendData:
        def load() -> TrendData:
            obj = self._ticker(market, ticker)
            try:
                history = obj.history(
                    period="5y",
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                )
                close = history.get("Close")
                raw_values = [] if close is None else close.tolist()
                values = [
                    value
                    for item in raw_values
                    if (value := _decimal(item)) is not None and value > 0
                ]
            except Exception as exc:
                raise MarketDataError("trend provider request failed") from exc
            if len(values) < 200:
                raise MarketDataError("trend provider returned insufficient history")

            one_year = values[-253:] if len(values) >= 253 else values
            return_1y = one_year[-1] / one_year[0] - Decimal("1")

            three_year = values[-757:] if len(values) >= 757 else values
            years = Decimal(len(three_year) - 1) / Decimal("252")
            ratio = three_year[-1] / three_year[0]
            cagr_3y = (ratio.ln() / years).exp() - Decimal("1") if years > 0 else None

            peak = three_year[0]
            max_drawdown = Decimal("0")
            for value in three_year:
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, value / peak - Decimal("1"))

            daily_returns = [
                one_year[index] / one_year[index - 1] - Decimal("1")
                for index in range(1, len(one_year))
            ]
            mean = sum(daily_returns, Decimal("0")) / Decimal(len(daily_returns))
            variance = sum(
                ((value - mean) ** 2 for value in daily_returns), Decimal("0")
            ) / Decimal(max(1, len(daily_returns) - 1))
            volatility = variance.sqrt() * Decimal("252").sqrt()
            average_200d = sum(values[-200:], Decimal("0")) / Decimal("200")
            return TrendData(
                market=market.upper(),
                ticker=ticker.upper(),
                as_of=datetime.now(timezone.utc),
                provider=self.name,
                observation_count=len(values),
                return_1y=return_1y,
                cagr_3y=cagr_3y,
                max_drawdown_3y=max_drawdown,
                annualized_volatility_1y=volatility,
                above_200d_average=values[-1] >= average_200d,
            )

        return await asyncio.to_thread(load)

    async def get_total_return_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 10,
    ) -> TotalReturnSeries:
        if not 1 <= lookback_years <= 30:
            raise MarketDataError("lookback years must be between 1 and 30")

        def load() -> TotalReturnSeries:
            obj = self._ticker(market, ticker)
            try:
                history = obj.history(
                    period=f"{lookback_years}y",
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                )
                close = history.get("Close")
                if close is None:
                    raise MarketDataError("total-return provider returned no close series")
                points: list[TotalReturnPoint] = []
                for index, raw_value in close.items():
                    value = _decimal(raw_value)
                    date_loader = getattr(index, "date", None)
                    raw_day = date_loader() if callable(date_loader) else index
                    if isinstance(raw_day, date) and value is not None and value > 0:
                        points.append(TotalReturnPoint(raw_day, value))
            except MarketDataError:
                raise
            except Exception as exc:
                raise MarketDataError("total-return provider request failed") from exc
            if len(points) < 2:
                raise MarketDataError("total-return provider returned insufficient history")
            currency = "KRW" if market.upper().startswith("KOS") else "USD"
            return TotalReturnSeries(
                market=market.upper(),
                ticker=ticker.upper(),
                currency=currency,
                as_of=datetime.now(timezone.utc),
                provider=self.name,
                series_kind="ADJUSTED_TOTAL_RETURN",
                coverage_start=points[0].trading_date,
                coverage_end=points[-1].trading_date,
                points=tuple(points),
            )

        return await asyncio.to_thread(load)

    async def get_distribution_series(
        self,
        market: str,
        ticker: str,
        *,
        lookback_years: int = 5,
    ) -> DistributionSeries:
        if not 1 <= lookback_years <= 30:
            raise MarketDataError("lookback years must be between 1 and 30")

        def load() -> DistributionSeries:
            obj = self._ticker(market, ticker)
            try:
                history = obj.history(
                    period=f"{lookback_years}y",
                    interval="1d",
                    auto_adjust=False,
                    actions=True,
                )
                if history.empty:
                    raise MarketDataError("distribution provider returned no history")
                first_index = history.index[0]
                last_index = history.index[-1]
                start_loader = getattr(first_index, "date", None)
                end_loader = getattr(last_index, "date", None)
                coverage_start = (
                    start_loader() if callable(start_loader) else first_index
                )
                coverage_end = end_loader() if callable(end_loader) else last_index
                if not isinstance(coverage_start, date) or not isinstance(coverage_end, date):
                    raise MarketDataError("distribution provider returned invalid dates")
                dividends = history.get("Dividends")
                points: list[DistributionPoint] = []
                if dividends is not None:
                    for index, raw_value in dividends.items():
                        value = _decimal(raw_value)
                        date_loader = getattr(index, "date", None)
                        raw_day = date_loader() if callable(date_loader) else index
                        if isinstance(raw_day, date) and value is not None and value > 0:
                            points.append(
                                DistributionPoint(
                                    ex_date=raw_day,
                                    amount_per_share=value,
                                )
                            )
            except MarketDataError:
                raise
            except Exception as exc:
                raise MarketDataError("distribution provider request failed") from exc
            currency = "KRW" if market.upper().startswith("KOS") else "USD"
            return DistributionSeries(
                market=market.upper(),
                ticker=ticker.upper(),
                currency=currency,
                as_of=datetime.now(timezone.utc),
                provider=self.name,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                points=tuple(points),
            )

        return await asyncio.to_thread(load)

    async def get_fx(self, base: str, quote: str) -> FxQuote:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return FxQuote(base, quote, Decimal("1"), datetime.now(timezone.utc), self.name)

        # yfinance's convenience wrapper occasionally reports a valid FX
        # symbol as missing. Yahoo's chart response is smaller and has proven
        # more reliable, so use it first and retain yfinance as a fallback.
        for symbol, inverse in yahoo_fx_candidates(base, quote):
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                try:
                    async with httpx.AsyncClient(
                        timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as client:
                        response = await client.get(
                            f"https://{host}/v8/finance/chart/{symbol}",
                            params={"range": "5d", "interval": "1d"},
                        )
                        response.raise_for_status()
                        result = response.json()["chart"]["result"][0]
                        meta = result["meta"]
                        rate = _decimal(meta.get("regularMarketPrice"))
                        if rate is None or rate <= 0:
                            continue
                        observed_raw = meta.get("regularMarketTime")
                        observed_at = (
                            datetime.fromtimestamp(int(observed_raw), tz=timezone.utc)
                            if observed_raw
                            else datetime.now(timezone.utc)
                        )
                        normalized = Decimal("1") / rate if inverse else rate
                        return FxQuote(
                            base,
                            quote,
                            normalized,
                            observed_at,
                            "yahoo-chart",
                        )
                except Exception:
                    continue

        def load() -> FxQuote:
            for symbol, inverse in yahoo_fx_candidates(base, quote):
                obj = self._ticker("FX", symbol)
                rate = None
                try:
                    rate = _decimal(obj.fast_info.get("last_price"))
                except Exception:
                    pass
                if rate is None or rate <= 0:
                    try:
                        history = obj.history(period="5d", interval="1d", actions=False)
                        close = history.get("Close")
                        if close is not None and len(close):
                            rate = _decimal(close.iloc[-1])
                    except Exception:
                        pass
                if rate is not None and rate > 0:
                    normalized = Decimal("1") / rate if inverse else rate
                    return FxQuote(
                        base,
                        quote,
                        normalized,
                        datetime.now(timezone.utc),
                        self.name,
                    )
            raise MarketDataError("FX provider returned no usable rate")

        return await asyncio.to_thread(load)

    async def get_fundamentals(self, market: str, ticker: str) -> FundamentalData:
        def load() -> FundamentalData:
            obj = self._ticker(market, ticker)
            try:
                info = obj.info or {}
            except Exception as exc:
                raise MarketDataError("fundamental provider request failed") from exc
            fields = {
                "revenue_growth": _decimal(info.get("revenueGrowth")),
                "earnings_growth": _decimal(info.get("earningsGrowth")),
                "gross_margin": _decimal(info.get("grossMargins")),
                "operating_margin": _decimal(info.get("operatingMargins")),
                "free_cash_flow": _decimal(info.get("freeCashflow")),
                "return_on_equity": _decimal(info.get("returnOnEquity")),
                "return_on_assets": _decimal(info.get("returnOnAssets")),
                "total_cash": _decimal(info.get("totalCash")),
                "total_debt": _decimal(info.get("totalDebt")),
                "debt_to_equity": _decimal(info.get("debtToEquity")),
                "market_cap": _decimal(info.get("marketCap")),
            }
            if not any(value is not None for value in fields.values()):
                raise MarketDataError("fundamental provider returned no usable metrics")
            return FundamentalData(
                market=market.upper(),
                ticker=ticker.upper(),
                as_of=datetime.now(timezone.utc),
                provider=self.name,
                **fields,
                sector=info.get("sector"),
                industry=info.get("industry"),
                country=info.get("country"),
                raw_coverage=_coverage(list(fields.values())),
            )

        return await asyncio.to_thread(load)

    async def get_valuation(self, market: str, ticker: str) -> ValuationData:
        def load() -> ValuationData:
            obj = self._ticker(market, ticker)
            try:
                info = obj.info or {}
                history = obj.history(
                    period="1mo",
                    interval="1d",
                    auto_adjust=False,
                    actions=False,
                )
            except Exception as exc:
                raise MarketDataError("valuation provider request failed") from exc
            close_series = history.get("Close") if history is not None else None
            rows: list[tuple[date, Decimal]] = []
            if close_series is not None:
                for index, value in close_series.items():
                    close = _decimal(value)
                    if close is None:
                        continue
                    date_loader = getattr(index, "date", None)
                    raw_day = date_loader() if callable(date_loader) else index
                    if isinstance(raw_day, date):
                        rows.append((raw_day, close))
            close_price, closed_at = _weekly_close(rows)

            shares = _decimal(info.get("sharesOutstanding"))
            market_cap = close_price * shares if shares and shares > 0 else None
            if market_cap is None:
                live_cap = _decimal(info.get("marketCap"))
                live_price = _decimal(
                    info.get("currentPrice") or info.get("regularMarketPrice")
                )
                if live_cap and live_price and live_price > 0:
                    market_cap = live_cap * close_price / live_price

            fcf = _decimal(info.get("freeCashflow"))
            fcf_yield = (
                fcf / market_cap if fcf is not None and market_cap and market_cap > 0 else None
            )
            total_debt = _decimal(info.get("totalDebt"))
            total_cash = _decimal(info.get("totalCash"))
            enterprise_value = (
                market_cap + (total_debt or Decimal("0")) - (total_cash or Decimal("0"))
                if market_cap is not None
                else None
            )
            if enterprise_value is None:
                live_ev = _decimal(info.get("enterpriseValue"))
                live_cap = _decimal(info.get("marketCap"))
                if live_ev is not None and live_cap is not None and market_cap is not None:
                    enterprise_value = live_ev - live_cap + market_cap
            ebitda = _decimal(info.get("ebitda"))
            revenue = _decimal(info.get("totalRevenue"))
            forward_eps = _decimal(info.get("forwardEps"))
            trailing_eps = _decimal(info.get("trailingEps"))
            latest_shares = prior_shares = None
            try:
                latest_shares, prior_shares = _statement_pair(
                    obj.balance_sheet,
                    ("Ordinary Shares Number", "Share Issued"),
                )
            except Exception:
                pass
            dilution_pct = (
                (latest_shares / prior_shares - Decimal("1")) * Decimal("100")
                if latest_shares is not None and prior_shares not in (None, Decimal("0"))
                else None
            )
            values = {
                "trailing_pe": (
                    close_price / trailing_eps if trailing_eps and trailing_eps > 0 else None
                ),
                "forward_pe": (
                    close_price / forward_eps if forward_eps and forward_eps > 0 else None
                ),
                "peg": _decimal(info.get("pegRatio")),
                "price_to_sales": (
                    market_cap / revenue if market_cap is not None and revenue and revenue > 0 else None
                ),
                "enterprise_to_ebitda": (
                    enterprise_value / ebitda
                    if enterprise_value is not None and ebitda and ebitda > 0
                    else None
                ),
                "enterprise_to_sales": (
                    enterprise_value / revenue
                    if enterprise_value is not None and revenue and revenue > 0
                    else None
                ),
                "free_cash_flow_yield": fcf_yield,
                "close_price": close_price,
                "dilution_pct": dilution_pct,
            }
            if not any(value is not None for value in values.values()):
                raise MarketDataError("valuation provider returned no usable metrics")
            return ValuationData(
                market=market.upper(),
                ticker=ticker.upper(),
                as_of=closed_at,
                provider="yfinance:weekly-close+stockbot-valuation",
                **values,
            )

        return await asyncio.to_thread(load)

    async def get_earnings(self, market: str, ticker: str) -> EarningsData:
        def load() -> EarningsData:
            obj = self._ticker(market, ticker)
            try:
                income = obj.quarterly_income_stmt
                cashflow = obj.quarterly_cashflow
            except Exception as exc:
                raise MarketDataError("earnings provider request failed") from exc
            if income is None or getattr(income, "empty", True):
                raise MarketDataError("earnings provider returned no quarterly statement")
            columns = list(income.columns)
            latest = columns[0]
            prior = columns[4] if len(columns) > 4 else None

            def statement_value(frame: Any, names: tuple[str, ...], column: Any) -> Decimal | None:
                if frame is None or getattr(frame, "empty", True) or column is None:
                    return None
                for name in names:
                    if name in frame.index:
                        return _decimal(frame.loc[name, column])
                return None

            revenue = statement_value(income, ("Total Revenue", "Operating Revenue"), latest)
            prior_revenue = statement_value(income, ("Total Revenue", "Operating Revenue"), prior)
            net_income = statement_value(
                income, ("Net Income", "Net Income Common Stockholders"), latest
            )
            prior_income = statement_value(
                income, ("Net Income", "Net Income Common Stockholders"), prior
            )
            gross_profit = statement_value(income, ("Gross Profit",), latest)
            operating_income = statement_value(income, ("Operating Income",), latest)
            fcf = statement_value(cashflow, ("Free Cash Flow",), latest)

            def growth(current: Decimal | None, old: Decimal | None) -> Decimal | None:
                return (
                    (current - old) / abs(old)
                    if current is not None and old not in (None, 0)
                    else None
                )

            return EarningsData(
                market=market.upper(),
                ticker=ticker.upper(),
                period=str(getattr(latest, "date", lambda: latest)()),
                announced_at=None,
                as_of=datetime.now(timezone.utc),
                provider=self.name,
                revenue=revenue,
                revenue_yoy=growth(revenue, prior_revenue),
                eps=None,
                eps_yoy=growth(net_income, prior_income),
                gross_margin=(
                    gross_profit / revenue if gross_profit is not None and revenue else None
                ),
                operating_margin=(
                    operating_income / revenue if operating_income is not None and revenue else None
                ),
                free_cash_flow=fcf,
            )

        return await asyncio.to_thread(load)

    async def get_news(self, market: str, ticker: str, *, limit: int = 10) -> list[NewsItem]:
        def load() -> list[NewsItem]:
            obj = self._ticker(market, ticker)
            try:
                rows = obj.news or []
            except Exception as exc:
                raise MarketDataError("news provider request failed") from exc
            items: list[NewsItem] = []
            for row in rows[: max(0, min(limit, 50))]:
                content = row.get("content", row)
                canonical = content.get("canonicalUrl") or {}
                clickthrough = content.get("clickThroughUrl") or {}
                url = canonical.get("url") or clickthrough.get("url") or row.get("link") or ""
                published = content.get("pubDate") or row.get("providerPublishTime")
                published_at: datetime | None = None
                if isinstance(published, (int, float)):
                    published_at = datetime.fromtimestamp(published, tz=timezone.utc)
                elif isinstance(published, str):
                    try:
                        published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    except ValueError:
                        published_at = None
                provider = content.get("provider") or {}
                title = str(content.get("title") or row.get("title") or "").strip()
                if not title:
                    continue
                items.append(
                    NewsItem(
                        ticker=ticker.upper(),
                        title=title,
                        url=str(url),
                        published_at=published_at,
                        publisher=provider.get("displayName")
                        if isinstance(provider, dict)
                        else str(provider),
                        summary=content.get("summary"),
                    )
                )
            return items

        return await asyncio.to_thread(load)
