"""Fail-closed Telegram authorization helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from functools import wraps
from typing import Any, TypeVar

Handler = TypeVar("Handler", bound=Callable[..., Awaitable[Any]])


def telegram_user_id(update: Any) -> int | None:
    """Extract a valid Telegram user ID without trusting arbitrary truthy data."""

    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    return user_id


def is_authorized_update(update: Any, allowed_user_ids: Collection[int]) -> bool:
    """Deny missing identities, group chats, and an empty allowlist.

    Account reports are private data. An allowlisted user invoking a command in
    a group must not cause the bot to publish the response to every group
    member, so authorization includes the destination chat boundary.
    """

    user_id = telegram_user_id(update)
    chat = getattr(update, "effective_chat", None)
    chat_type = str(getattr(chat, "type", "")).lower()
    chat_id = getattr(chat, "id", None)
    return (
        bool(allowed_user_ids)
        and user_id is not None
        and user_id in allowed_user_ids
        and chat_type == "private"
        and isinstance(chat_id, int)
        and not isinstance(chat_id, bool)
        and chat_id == user_id
    )


async def reject_unauthorized(update: Any) -> None:
    """Return a generic response that reveals no account or allowlist detail."""

    message = getattr(update, "effective_message", None)
    reply = getattr(message, "reply_text", None)
    if callable(reply):
        await reply("이 계좌관리봇을 사용할 권한이 없습니다.")


def authorized_handler(
    allowed_user_ids: Collection[int],
    callback: Handler,
    *,
    accepting: Callable[[], bool] | None = None,
) -> Handler:
    """Wrap a python-telegram-bot callback with deny-by-default authorization."""

    @wraps(callback)
    async def guarded(update: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_authorized_update(update, allowed_user_ids):
            await reject_unauthorized(update)
            return None
        if accepting is not None and not accepting():
            message = getattr(update, "effective_message", None)
            if message is not None:
                await message.reply_text("계좌관리봇이 종료 중입니다. 잠시 후 다시 시도해 주세요.")
            return None
        return await callback(update, context, *args, **kwargs)

    return guarded  # type: ignore[return-value]
