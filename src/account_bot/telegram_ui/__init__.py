"""Telegram command surface for the read-only account bot."""

from .auth import authorized_handler, is_authorized_update, telegram_user_id
from .handlers import (
    RecurringPlanGateway,
    TelegramCommandController,
    UnavailableRecurringPlanGateway,
    register_handlers,
)

__all__ = [
    "RecurringPlanGateway",
    "TelegramCommandController",
    "UnavailableRecurringPlanGateway",
    "authorized_handler",
    "is_authorized_update",
    "register_handlers",
    "telegram_user_id",
]
