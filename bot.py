#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск сообщений через отдельный авторизованный аккаунт Telegram."""

import asyncio
import logging
import os
import time
from html import escape as html_escape
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import User

from search_service import SearchRunner

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")
MAX_PER_CHAT = int(os.getenv("MAX_PER_CHAT", "300"))
MAX_CHATS = int(os.getenv("MAX_CHATS", "300"))

if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_ID]):
    raise ValueError("Заполните API_ID, API_HASH, BOT_TOKEN и ADMIN_ID!")

HELP_TEXT = (
    "Отправьте одно из значений:\n"
    "• <code>@nickname</code> — по нику\n"
    "• <code>123456789</code> — по id\n"
    "• <code>+7 999 123-45-67</code> — по телефону"
)


def fmt_user(user: User) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    return name or str(user.id)


class DeepSearchBot:
    def __init__(self) -> None:
        if not USER_SESSION_STRING:
            raise ValueError("Добавьте USER_SESSION_STRING в закрытые переменные Railway.")
        self.user = TelegramClient(
            StringSession(USER_SESSION_STRING), API_ID, API_HASH,
            flood_sleep_threshold=0, request_retries=0,
        )
        self.bot = TelegramClient(StringSession(), API_ID, API_HASH)
        self.busy = False
        self.next_search_at = 0
        self.owner_ids = {ADMIN_ID}

    # ---------------------------------------------------------------- запуск
    async def start(self) -> None:
        await self.user.connect()
        if not await self.user.is_user_authorized():
            raise RuntimeError(
                "Сессия аккаунта-сканера не авторизована. "
                "Обновите USER_SESSION_STRING в закрытых переменных Railway."
            )

        me = await self.user.get_me()
        self.owner_ids.add(me.id)
        logger.info("Сканер: %s (ID: %s)", fmt_user(me), me.id)
        logger.info("Владельцы бота: %s", sorted(self.owner_ids))

        await self.bot.start(bot_token=BOT_TOKEN)
        bot_me = await self.bot.get_me()
        logger.info("Бот: @%s (ID: %s)", bot_me.username, bot_me.id)

        self.bot.add_event_handler(self.cmd_start, events.NewMessage(pattern=r"^/start"))
        self.bot.add_event_handler(self.cmd_help, events.NewMessage(pattern=r"^/help"))
        self.bot.add_event_handler(
            self.cmd_search, events.NewMessage(pattern=r"^/(?:search|find)(?:\s|$)")
        )
        self.bot.add_event_handler(self.cmd_any, events.NewMessage(incoming=True))

        logger.info("Готов к поиску")
        await self.bot.run_until_disconnected()

    # ------------------------------------------------------------ обработчики
    async def cmd_start(self, event: events.NewMessage.Event) -> None:
        logger.info("Входящее /start от ID %s", event.sender_id)
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        await event.respond(HELP_TEXT, parse_mode="html")

    async def cmd_help(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        await event.respond(HELP_TEXT, parse_mode="html")

    async def cmd_search(self, event: events.NewMessage.Event) -> None:
        logger.info("Входящее /search от ID %s", event.sender_id)
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        parts = (event.message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await event.respond(
                "Напишите так: <code>/search @nickname</code>", parse_mode="html"
            )
            return
        await self.run_search(event, parts[1].strip())

    async def cmd_any(self, event: events.NewMessage.Event) -> None:
        text = (event.message.text or "").strip()
        logger.info("Входящее от ID %s", event.sender_id)
        if not text or text.startswith("/"):
            return
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        await self.run_search(event, text)

    def is_admin(self, event: events.NewMessage.Event) -> bool:
        return event.is_private and event.sender_id in self.owner_ids

    # --------------------------------------------------------------- сервис
    async def resolve_user(self, query: str) -> Optional[User]:
        try:
            if query.startswith("+"):
                digits = "".join(ch for ch in query if ch.isdigit())
                contacts = await self.user(functions.contacts.GetContactsRequest(hash=0))
                for candidate in contacts.users:
                    if candidate.phone and candidate.phone == digits:
                        return candidate
                return None
            entity = await self.user.get_entity(int(query) if query.isdigit() else query)
            return entity if isinstance(entity, User) else None
        except FloodWaitError:
            raise
        except ValueError:
            return None

    async def deep_search(self, user: User, status=None) -> dict:
        async def progress(text):
            if status:
                try:
                    await status.edit(text, parse_mode=None)
                except RPCError:
                    pass

        runner = SearchRunner(
            self.user, user, max_chats=MAX_CHATS, per_chat=MAX_PER_CHAT,
            progress=progress,
        )
        result = await runner.run()
        self.next_search_at = time.monotonic() + result["retry_after"]
        return result

    # ------------------------------------------------------------ оформление
    def format_results(self, result: dict, per_chat: int = 3) -> list[str]:
        user = result["user"]
        header = (
            f"🔍 <b>Результаты глубокого поиска для {html_escape(fmt_user(user))}</b>\n"
            f"👤 User ID: <code>{user.id}</code>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Всего сообщений: <b>{result['total_messages']}</b>\n"
            f"• Всего чатов: <b>{len(result['chats'])}</b>\n"
            f"• Публичных: <b>{len(result['public'])}</b>\n"
            f"• Приватных: <b>{len(result['private'])}</b>\n"
            f"• Найдено источников вне подписок: <b>{result['external_found']}</b>\n"
            f"• Проверено групп: <b>{result['checked']}</b>\n"
            f"• Из них вне подписок: <b>{result['external_checked']}</b>\n"
            f"• Вне подписок с сообщениями автора: <b>{len(result['discovered'])}</b>\n"
            f"• Упоминаний отдельно: <b>{len(result['mentions'])}</b>\n\n"
        )
        blocks = []
        if result["notes"]:
            blocks.append("<b>Ограничения поиска:</b>\n" + "\n".join(
                "• " + html_escape(note) for note in result["notes"]
            ) + "\n\n")
        if not result["chats"]:
            blocks.append("Сообщения с подтверждённым авторством в проверенных источниках не найдены.\n\n")
        sections = [
            ("🔓 <b>ПУБЛИЧНЫЕ ГРУППЫ/КАНАЛЫ:</b>", result["public"]),
            ("🔒 <b>ПРИВАТНЫЕ ГРУППЫ:</b>", result["private"]),
        ]
        for title, chats in sections:
            if not chats:
                continue
            blocks.append(title + "\n")
            for chat in chats[:15]:
                badge = "🆕 " if chat["source"] != "dialogs" else ""
                lines = [
                    f"{badge}<b>{html_escape(chat['name'])}</b>",
                    f"📝 Сообщений: {chat['count']}",
                ]
                for message in chat["messages"][:per_chat]:
                    date = message["date"].strftime("%d.%m.%Y %H:%M")
                    text = html_escape(message["text"][:70])
                    if len(message["text"]) > 70:
                        text += "…"
                    lines.append(f"• <code>{date}</code> {text}")
                    if message["link"]:
                        lines.append(f'  <a href="{message["link"]}">→ Перейти</a>')
                if chat["count"] > per_chat:
                    lines.append(f"… +{chat['count'] - per_chat} ещё")
                blocks.append("\n".join(lines) + "\n\n")
            if len(chats) > 15:
                blocks.append(f"Ещё {len(chats) - 15} групп с результатами не показаны в кратком отчёте.\n\n")

        if result["mentions"]:
            blocks.append("<b>Упоминания — не сообщения этого автора:</b>\n\n")
            for item in result["mentions"][:10]:
                text = html_escape(item["text"][:180])
                block = f"<b>{html_escape(item['chat'])}</b>\n{text}\n"
                if item["link"]:
                    block += f'<a href="{html_escape(item["link"], quote=True)}">Открыть</a>\n'
                blocks.append(block + "\n")
            if len(result["mentions"]) > 10:
                blocks.append(f"Ещё упоминаний: {len(result['mentions']) - 10}.\n")

        return self.split(header, blocks)

    @staticmethod
    def split(header: str, blocks: list[str], limit: int = 3600) -> list[str]:
        parts = []
        current = header
        for block in blocks:
            if len(current) + len(block) > limit and current.strip():
                parts.append(current)
                current = ""
            current += block
        if current.strip():
            parts.append(current)
        return parts

    # -------------------------------------------------------------- сценарий
    async def run_search(self, event: events.NewMessage.Event, query: str) -> None:
        if self.busy:
            await event.respond("⏳ Уже ищу, подождите.")
            return

        remaining = self.next_search_at - time.monotonic()
        if remaining > 0:
            await event.respond(f"Telegram временно ограничил запросы. Повторите через {int(remaining) + 1} с.")
            return
        if len(query) > 200:
            await event.respond("Запрос слишком длинный. Отправьте ник, id или телефон.")
            return

        self.busy = True
        status = None
        try:
            status = await event.respond(
                f"🔍 <b>Запускаю глубокий поиск…</b>\n\n"
                f"Запрос: <code>{html_escape(query)}</code>",
                parse_mode="html",
            )

            user = await asyncio.wait_for(self.resolve_user(query), timeout=30)
            if not user:
                await status.edit(
                    "❌ <b>Пользователь не найден</b>\n\n"
                    "Проверьте ник, номер или ID.",
                    parse_mode="html",
                )
                return

            await status.edit(
                f"✅ <b>Пользователь найден:</b> {html_escape(fmt_user(user))}\n\n"
                "Сканирую чаты… ⏳",
                parse_mode="html",
            )

            result = await self.deep_search(user, status)
            parts = self.format_results(result)

            try:
                await status.delete()
            except Exception:
                pass

            for index, part in enumerate(parts, 1):
                prefix = (
                    f"📄 <b>Часть {index}/{len(parts)}</b>\n\n"
                    if len(parts) > 1
                    else ""
                )
                await event.respond(prefix + part, parse_mode="html")
                await asyncio.sleep(0.4)

            conclusion = "Поиск завершён с ограничениями." if result["notes"] else "Поиск завершён!"
            await event.respond(
                f"<b>{conclusion}</b>\n\n"
                f"Найдено <b>{result['total_messages']}</b> сообщений "
                f"в <b>{len(result['chats'])}</b> чатах.",
                parse_mode="html",
            )
        except FloodWaitError as error:
            self.next_search_at = time.monotonic() + error.seconds
            await event.respond(f"Telegram временно ограничил запросы. Повторите через {error.seconds} с.")
        except Exception as error:
            logger.error("Ошибка поиска: %s", type(error).__name__)
            text = "Не удалось завершить поиск. Попробуйте позже."
            try:
                if status:
                    await status.edit(text, parse_mode="html")
                else:
                    await event.respond(text, parse_mode="html")
            except Exception:
                pass
        finally:
            self.busy = False


async def main() -> None:
    bot = DeepSearchBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Остановлено")
    except Exception as error:
        logger.error("Критическая ошибка: %s", error)
        raise


if __name__ == "__main__":
    asyncio.run(main())
