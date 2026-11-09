import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

with patch.dict(os.environ, {"API_ID": "1", "API_HASH": "test", "BOT_TOKEN": "test", "ADMIN_ID": "1"}), patch("dotenv.load_dotenv"):
    import bot

from telethon import types
from telethon.errors import FloodWaitError
from telethon.extensions import html
from telethon.sessions import StringSession
from telethon.crypto import AuthKey


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = bot.DeepSearchBot.__new__(bot.DeepSearchBot)
        self.bot.busy = False
        self.bot.owner_ids = {1}
        self.bot.next_search_at = 0
        self.user = types.User(id=100, first_name="Test", username="testuser")
        self.result = {
            "user": self.user, "chats": [], "public": [], "private": [],
            "total_messages": 0, "discovered": [], "external_found": 2,
            "checked": 2, "external_checked": 2, "mentions": [],
            "notes": [], "retry_after": 0,
        }
        self.status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.event = SimpleNamespace(
            sender_id=1, is_private=True, respond=AsyncMock(return_value=self.status),
            message=SimpleNamespace(text="@testuser"),
        )

    def test_missing_session_never_opens_file_or_client(self):
        with patch("bot.USER_SESSION_STRING", ""), patch("bot.TelegramClient") as client:
            with self.assertRaisesRegex(ValueError, "USER_SESSION_STRING"):
                bot.DeepSearchBot()
        client.assert_not_called()

    def test_clients_use_only_memory_sessions(self):
        session = StringSession()
        session.set_dc(2, "149.154.167.51", 443)
        session.auth_key = AuthKey(bytes(range(256)))
        with patch("bot.USER_SESSION_STRING", session.save()), patch("bot.TelegramClient") as client:
            bot.DeepSearchBot()
        self.assertEqual(client.call_count, 2)
        for call in client.call_args_list:
            self.assertIsInstance(call.args[0], StringSession)
        self.assertEqual(client.call_args_list[0].kwargs["flood_sleep_threshold"], 0)
        self.assertIsNone(client.call_args_list[1].args[0].auth_key)

    async def test_revoked_session_does_not_start_bot(self):
        self.bot.user = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False))
        self.bot.bot = SimpleNamespace(start=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "USER_SESSION_STRING"):
            await self.bot.start()
        self.bot.bot.start.assert_not_awaited()

    async def test_start_text_unchanged(self):
        await self.bot.cmd_start(self.event)
        text = html.parse(self.event.respond.call_args.args[0])[0]
        self.assertEqual(text, "Отправьте одно из значений:\n• @nickname — по нику\n• 123456789 — по id\n• +7 999 123-45-67 — по телефону")

    async def test_group_cannot_receive_private_search_results(self):
        self.event.is_private = False
        self.bot.run_search = AsyncMock()
        await self.bot.cmd_any(self.event)
        self.bot.run_search.assert_not_awaited()
        self.assertFalse(self.bot.is_admin(self.event))

    async def test_unauthorized_user(self):
        self.event.sender_id = 22
        self.bot.run_search = AsyncMock()
        await self.bot.cmd_any(self.event)
        self.bot.run_search.assert_not_awaited()
        self.assertIn("Доступ к поиску не выдан", self.event.respond.call_args.args[0])

    async def test_cooldown_prevents_calls(self):
        self.bot.next_search_at = time.monotonic() + 100
        self.bot.resolve_user = AsyncMock()
        await self.bot.run_search(self.event, "@testuser")
        self.bot.resolve_user.assert_not_awaited()
        self.assertIn("ограничил", self.event.respond.call_args.args[0])

    async def test_busy_search_is_not_started_twice(self):
        self.bot.busy = True
        self.bot.resolve_user = AsyncMock()
        await self.bot.run_search(self.event, "@testuser")
        self.bot.resolve_user.assert_not_awaited()

    async def test_floodwait_during_resolution_releases_busy_and_sets_cooldown(self):
        self.bot.resolve_user = AsyncMock(side_effect=FloodWaitError(request=None, capture=75))
        await self.bot.run_search(self.event, "@testuser")
        self.assertFalse(self.bot.busy)
        self.assertGreater(self.bot.next_search_at, time.monotonic() + 70)

    async def test_handler_delivers_partial_report_and_mentions(self):
        self.result["notes"] = ["Поиск публикаций недоступен."]
        self.result["mentions"] = [{"chat": "<channel>", "text": "someone said @testuser & <b>hi</b>", "link": None}]
        self.bot.resolve_user = AsyncMock(return_value=self.user)
        self.bot.deep_search = AsyncMock(return_value=self.result)
        with patch("bot.asyncio.sleep", new=AsyncMock()):
            await self.bot.run_search(self.event, "@testuser")
        self.assertFalse(self.bot.busy)
        texts = [call.args[0] for call in self.event.respond.call_args_list]
        self.assertIn("с ограничениями", texts[-1])
        report = "\n".join(texts)
        self.assertIn("Упоминания — не сообщения этого автора", report)
        self.assertIn("&lt;channel&gt;", report)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", report)
        self.assertIn("Всего сообщений: <b>0</b>", report)
        self.assertIn("Из них вне подписок: <b>2</b>", report)

    async def test_unknown_user_releases_busy(self):
        self.bot.resolve_user = AsyncMock(return_value=None)
        await self.bot.run_search(self.event, "@unknown")
        self.assertFalse(self.bot.busy)
        self.assertIn("Пользователь не найден", self.status.edit.call_args.args[0])

    async def test_request_too_long_never_resolves(self):
        self.bot.resolve_user = AsyncMock()
        await self.bot.run_search(self.event, "a" * 201)
        self.bot.resolve_user.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
