"""Future webhook admission must fail closed without network or secrets."""

from __future__ import annotations

import json

from account_bot.cloud_webhook import MAX_UPDATE_BYTES, admitted_update_id

SECRET = "fixture-secret"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


def _body(*, chat_type: str = "private", user_id: object = 42, chat_id: object = 42) -> bytes:
    return json.dumps({
        "update_id": 73,
        "message": {"from": {"id": user_id}, "chat": {"id": chat_id, "type": chat_type}},
    }).encode()


def test_admits_private_allowlisted_message_and_callback() -> None:
    assert admitted_update_id(HEADERS, _body(), expected_secret=SECRET, allowed_user_ids={42}) == 73
    callback = json.dumps({
        "update_id": 74,
        "callback_query": {
            "from": {"id": 42},
            "message": {"chat": {"id": 42, "type": "private"}},
        },
    }).encode()
    assert admitted_update_id(
        HEADERS, callback, expected_secret=SECRET, allowed_user_ids={42}
    ) == 74


def test_rejects_missing_bad_or_ambiguous_secret() -> None:
    for headers in (
        {}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        {"X-Telegram-Bot-Api-Secret-Token": SECRET,
         "x-telegram-bot-api-secret-token": SECRET},
    ):
        assert admitted_update_id(
            headers, _body(), expected_secret=SECRET, allowed_user_ids={42}
        ) is None
    assert admitted_update_id(HEADERS, _body(), expected_secret="", allowed_user_ids={42}) is None


def test_rejects_group_other_identity_invalid_json_and_oversize() -> None:
    bodies = [
        _body(chat_type="group"), _body(user_id=43), _body(chat_id=43),
        _body(user_id=True), b"{", b"x" * (MAX_UPDATE_BYTES + 1),
        json.dumps({"update_id": True, "message": {}}).encode(),
    ]
    for body in bodies:
        assert admitted_update_id(
            HEADERS, body, expected_secret=SECRET, allowed_user_ids={42}
        ) is None
    assert admitted_update_id(HEADERS, _body(), expected_secret=SECRET, allowed_user_ids=()) is None
