"""UTC Cron ticks map to the existing KST report times without side effects."""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from account_bot.cloud_schedule import due_jobs


def _due(value: datetime) -> tuple[str, ...]:
    return due_jobs(
        value, timezone_name="Asia/Seoul", daily_time=time(8),
        weekly_time=time(9), weekly_weekday=5,
    )


def test_default_utc_cron_schedule() -> None:
    assert _due(datetime(2026, 10, 25, 23, tzinfo=timezone.utc)) == ("daily",)
    assert _due(datetime(2026, 10, 30, 23, 30, tzinfo=timezone.utc)) == ("weekly_valuation",)
    assert _due(datetime(2026, 10, 31, 0, tzinfo=timezone.utc)) == ("weekly",)
    assert _due(datetime(2026, 10, 31, 23, tzinfo=timezone.utc)) == ()


def test_schedule_rejects_naive_or_invalid_weekday() -> None:
    with pytest.raises(ValueError, match="aware"):
        _due(datetime(2026, 10, 25, 23))
    with pytest.raises(ValueError, match="weekday"):
        due_jobs(
            datetime(2026, 10, 25, 23, tzinfo=timezone.utc),
            timezone_name="Asia/Seoul", daily_time=time(8),
            weekly_time=time(9), weekly_weekday=7,
        )


def test_schedule_uses_configured_timezone_and_time() -> None:
    value = datetime(2026, 10, 26, 12, 30, tzinfo=timezone.utc)
    assert due_jobs(
        value, timezone_name="America/New_York", daily_time=time(8, 30),
        weekly_time=time(9), weekly_weekday=5,
    ) == ("daily",)
    assert due_jobs(
        datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc),
        timezone_name="America/New_York", daily_time=time(8, 30),
        weekly_time=time(9), weekly_weekday=5,
    ) == ("daily",)
