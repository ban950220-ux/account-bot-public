from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_bot.telegram_ui.auth import (  # noqa: E402
    authorized_handler,
    is_authorized_update,
)


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str) -> None:
        self.id = chat_id
        self.type = chat_type


class FakeUpdate:
    def __init__(
        self,
        user_id: int | None,
        *,
        chat_id: int | None = None,
        chat_type: str = "private",
    ) -> None:
        self.effective_user = FakeUser(user_id) if user_id is not None else None
        resolved_chat_id = user_id if chat_id is None else chat_id
        self.effective_chat = (
            FakeChat(resolved_chat_id, chat_type) if resolved_chat_id is not None else None
        )
        self.effective_message = FakeMessage()


class TelegramAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_started_before_shutdown_finishes_without_cancellation(self) -> None:
        update = FakeUpdate(1001)
        entered = asyncio.Event()
        release = asyncio.Event()
        accepting = True

        async def callback(received_update, context):
            del received_update, context
            entered.set()
            await release.wait()
            return "completed"

        guarded = authorized_handler({1001}, callback, accepting=lambda: accepting)
        task = asyncio.create_task(guarded(update, object()))
        await entered.wait()
        accepting = False
        release.set()

        self.assertEqual(await task, "completed")
        self.assertEqual(update.effective_message.replies, [])

    async def test_stopping_acknowledges_new_authorized_command_without_running_it(self) -> None:
        update = FakeUpdate(1001)
        called = False

        async def callback(received_update, context):
            nonlocal called
            called = True

        await authorized_handler({1001}, callback, accepting=lambda: False)(update, object())

        self.assertFalse(called)
        self.assertEqual(
            update.effective_message.replies,
            ["계좌관리봇이 종료 중입니다. 잠시 후 다시 시도해 주세요."],
        )

    async def test_allowed_user_reaches_handler_without_rejection(self) -> None:
        update = FakeUpdate(1001)
        calls: list[int] = []

        async def callback(received_update, context):
            calls.append(received_update.effective_user.id)
            return "handled"

        guarded = authorized_handler({1001, 1002}, callback)
        result = await guarded(update, object())

        self.assertTrue(is_authorized_update(update, {1001, 1002}))
        self.assertEqual(result, "handled")
        self.assertEqual(calls, [1001])
        self.assertEqual(update.effective_message.replies, [])

    async def test_unlisted_user_is_rejected_without_running_handler(self) -> None:
        update = FakeUpdate(9999)
        called = False

        async def callback(received_update, context):
            nonlocal called
            called = True

        guarded = authorized_handler({1001}, callback)
        result = await guarded(update, object())

        self.assertFalse(is_authorized_update(update, {1001}))
        self.assertIsNone(result)
        self.assertFalse(called)
        self.assertEqual(len(update.effective_message.replies), 1)
        self.assertIn("권한이 없습니다", update.effective_message.replies[0])
        self.assertNotIn("9999", update.effective_message.replies[0])

    async def test_empty_allowlist_fails_closed_even_for_valid_identity(self) -> None:
        update = FakeUpdate(1001)
        calls = 0

        async def callback(received_update, context):
            nonlocal calls
            calls += 1

        guarded = authorized_handler(set(), callback)
        result = await guarded(update, object())

        self.assertFalse(is_authorized_update(update, set()))
        self.assertIsNone(result)
        self.assertEqual(calls, 0)
        self.assertEqual(
            update.effective_message.replies,
            ["이 계좌관리봇을 사용할 권한이 없습니다."],
        )

    async def test_allowlisted_user_is_blocked_in_group_chat(self) -> None:
        update = FakeUpdate(1001, chat_id=-5001, chat_type="group")
        calls = 0

        async def callback(received_update, context):
            nonlocal calls
            calls += 1

        result = await authorized_handler({1001}, callback)(update, object())

        self.assertFalse(is_authorized_update(update, {1001}))
        self.assertIsNone(result)
        self.assertEqual(calls, 0)
        self.assertEqual(len(update.effective_message.replies), 1)

    async def test_private_chat_id_must_match_allowlisted_user(self) -> None:
        update = FakeUpdate(1001, chat_id=2002, chat_type="private")

        self.assertFalse(is_authorized_update(update, {1001}))


if __name__ == "__main__":
    unittest.main()
