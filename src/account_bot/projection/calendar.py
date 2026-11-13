"""Trading-calendar abstraction used by date-based projections."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta


class TradingCalendarProvider(ABC):
    name: str
    version: str

    @abstractmethod
    def count_trading_days(self, start: date, end: date) -> int:
        """Count sessions after ``start`` through and including ``end``."""

    @abstractmethod
    def get_trading_days(self, start: date, end: date) -> tuple[date, ...]:
        """Return sessions after ``start`` through and including ``end``."""


class XNYSTradingCalendar(TradingCalendarProvider):
    name = "US Equity Market Calendar (XNYS)"

    def __init__(self) -> None:
        try:
            import exchange_calendars
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("exchange-calendars is required") from exc
        self._module = exchange_calendars
        self.version = getattr(exchange_calendars, "__version__", "unknown")

    def count_trading_days(self, start: date, end: date) -> int:
        return len(self.get_trading_days(start, end))

    def get_trading_days(self, start: date, end: date) -> tuple[date, ...]:
        if end <= start:
            return ()
        calendar = self._module.get_calendar(
            "XNYS",
            start=start - timedelta(days=10),
            end=end + timedelta(days=10),
        )
        sessions = calendar.sessions_in_range(start + timedelta(days=1), end)
        return tuple(session.date() for session in sessions)
