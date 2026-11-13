"""Credential-free admission boundary for a future Telegram webhook adapter.

This module does not register a webhook or execute a Telegram command. The
durable update claim and response path must be implemented before use.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Collection, Mapping
from typing import Any

MAX_UPDATE_BYTES = 64 * 1024


def _positive_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def admitted_update_id(
    headers: Mapping[str, str],
    body: bytes,
    *,
    expected_secret: str,
    allowed_user_ids: Collection[int],
) -> int | None:
    """Return a private allowlisted update ID, or deny without processing it."""
    if not expected_secret or not allowed_user_ids or len(body) > MAX_UPDATE_BYTES:
        return None
    values = [
        value for key, value in headers.items()
        if key.lower() == "x-telegram-bot-api-secret-token"
    ]
    if len(values) != 1 or not hmac.compare_digest(values[0], expected_secret):
        return None
    try:
        update = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(update, dict):
        return None
    update_id = _positive_id(update.get("update_id"))
    if update_id is None:
        return None
    message = update.get("message")
    if isinstance(message, dict):
        user = message.get("from")
        chat = message.get("chat")
    else:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return None
        user = callback.get("from")
        callback_message = callback.get("message")
        chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
    if not isinstance(user, dict) or not isinstance(chat, dict):
        return None
    user_id = _positive_id(user.get("id"))
    chat_id = _positive_id(chat.get("id"))
    if user_id is None or user_id not in allowed_user_ids:
        return None
    if chat.get("type") != "private" or chat_id != user_id:
        return None
    return update_id
