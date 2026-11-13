"""Pure due-job check for future Cloudflare Cron triggers.

Cloudflare delivers UTC scheduled times. This module evaluates the configured
local reporting day without starting a PTB JobQueue or making an API call.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def due_jobs(
    scheduled_at: datetime,
    *,
    timezone_name: str,
    daily_time: time,
    weekly_time: time,
    weekly_weekday: int,
) -> tuple[str, ...]:
    """Return daily, weekly valuation, or weekly report for an exact Cron tick."""
    if scheduled_at.tzinfo is None or not 0 <= weekly_weekday <= 6:
        raise ValueError("scheduled time must be aware and weekday valid")
    local = scheduled_at.astimezone(ZoneInfo(timezone_name))
    clock = local.timetz().replace(tzinfo=None)
    jobs = []
    if local.weekday() < 5 and clock == daily_time:
        jobs.append("daily")
    if local.weekday() == 5 and clock == time(8, 30):
        jobs.append("weekly_valuation")
    if local.weekday() == weekly_weekday and clock == weekly_time:
        jobs.append("weekly")
    return tuple(jobs)
