#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск сообщений через отдельный авторизованный аккаунт Telegram."""

import asyncio
import logging
import os
import re
import time
from html import escape as html_escape
from io import BytesIO
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, User

from search_service import SearchRunner, PhraseSearchRunner, message_link

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
    "• <code>+7 999 123-45-67</code> — по телефону\n\n"
    "Команды:\n"
    "• <code>/web фраза</code> — поиск по фразе в открытых группах/каналах\n"
    "• <code>/addwords слова</code> — добавить слова для мониторинга (или .txt файл)\n"
    "• <code>/words</code> — показать список слов\n"
    "• <code>/delword слово</code> — удалить слово\n"
    "• <code>/clearwords</code> — очистить список"
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
        self.scanner_id = None
        self.watch_words = []

    # ---------------------------------------------------------------- запуск
    async def start(self) -> None:
        await self.user.connect()
        if not await self.user.is_user_authorized():
            raise RuntimeError(
                "Сессия аккаунта-сканера не авторизована. "
                "Обновите USER_SESSION_STRING в закрытых переменных Railway."
            )

        me = await self.user.get_me()
        self.scanner_id = me.id
        self.owner_ids.add(me.id)
        logger.info("Сканер: %s (ID: %s)", fmt_user(me), me.id)
        logger.info("Владельцы бота: %s", sorted(self.owner_ids))

        self.watch_words = await self.load_watch_words()
        logger.info("Слов для мониторинга: %s", len(self.watch_words))

        await self.bot.start(bot_token=BOT_TOKEN)
        bot_me = await self.bot.get_me()
        logger.info("Бот: @%s (ID: %s)", bot_me.username, bot_me.id)

        self.user.add_event_handler(self.on_group_message, events.NewMessage(incoming=True))

        self.bot.add_event_handler(self.cmd_start, events.NewMessage(pattern=r"^/start"))
        self.bot.add_event_handler(self.cmd_help, events.NewMessage(pattern=r"^/help"))
        self.bot.add_event_handler(
            self.cmd_search, events.NewMessage(pattern=r"^/(?:search|find)(?:\s|$)")
        )
        self.bot.add_event_handler(self.cmd_web, events.NewMessage(pattern=r"^/web(?:\s|$)"))
        self.bot.add_event_handler(self.cmd_addwords, events.NewMessage(pattern=r"^/addwords(?:\s|$)"))
        self.bot.add_event_handler(self.cmd_words, events.NewMessage(pattern=r"^/words$"))
        self.bot.add_event_handler(self.cmd_delword, events.NewMessage(pattern=r"^/delword(?:\s|$)"))
        self.bot.add_event_handler(self.cmd_clearwords, events.NewMessage(pattern=r"^/clearwords$"))
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

    async def cmd_web(self, event: events.NewMessage.Event) -> None:
        logger.info("Входящее /web от ID %s", event.sender_id)
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
                "Напишите так: <code>/web Дзин Пиня</code>", parse_mode="html"
            )
            return
        await self.run_phrase_search(event, parts[1].strip())

    # ------------------------------------------------------ мониторинг слов
    @staticmethod
    def parse_words(text: str) -> list[str]:
        if not text:
            return []
        words = []
        for line in text.splitlines():
            for token in re.split(r"[,\s]+", line):
                token = token.strip().lower()
                if token:
                    words.append(token)
        return words

    def match_words(self, text: str) -> Optional[str]:
        if not text or not self.watch_words:
            return None
        for word in self.watch_words:
            pattern = r"(?i)(?<!\w)" + re.escape(word) + r"(?!\w)"
            if re.search(pattern, text):
                return word
        return None

    async def load_watch_words(self) -> list[str]:
        try:
            async for message in self.user.iter_messages("me", search="#watchwords", limit=1):
                if message.message and message.message.startswith("#watchwords"):
                    return self.parse_words(message.message[len("#watchwords"):].strip())
        except Exception as error:
            logger.error("Ошибка загрузки списка слов: %s", error)
        return []

    async def save_watch_words(self) -> None:
        text = "#watchwords\n" + "\n".join(sorted(self.watch_words))
        try:
            async for message in self.user.iter_messages("me", search="#watchwords", limit=1):
                if message.message and message.message.startswith("#watchwords"):
                    await self.user.edit_message("me", message.id, text)
                    return
        except Exception as error:
            logger.error("Ошибка обновления списка слов: %s", error)
        try:
            await self.user.send_message("me", text)
        except Exception as error:
            logger.error("Ошибка сохранения списка слов: %s", error)

    async def cmd_addwords(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        words = []
        parts = (event.message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            words.extend(self.parse_words(parts[1]))
        document = getattr(event.message, "document", None)
        if document and getattr(document, "mime_type", "").startswith("text/"):
            size = getattr(document, "size", 0)
            if size > 1024 * 1024:
                await event.respond("Файл слишком большой. Максимум 1 МБ.")
                return
            try:
                buffer = BytesIO()
                await event.message.download_media(file=buffer)
                buffer.seek(0)
                content = buffer.read().decode("utf-8", errors="replace")
                words.extend(self.parse_words(content))
            except Exception as error:
                logger.error("Ошибка чтения файла: %s", error)
                await event.respond("Не удалось прочитать файл.")
                return
        words = [w for w in words if w]
        if not words:
            await event.respond(
                "Не найдены слова.\n"
                "Отправьте так: <code>/addwords слово1 слово2</code>\n"
                "или прикрепите .txt файл с словами.",
                parse_mode="html",
            )
            return
        existing = set(self.watch_words)
        added = [w for w in words if w not in existing]
        self.watch_words = sorted(set(self.watch_words) | set(words))
        await self.save_watch_words()
        await event.respond(
            f"Добавлено слов: <b>{len(added)}</b>.\n"
            f"Всего в списке: <b>{len(self.watch_words)}</b>.",
            parse_mode="html",
        )

    async def cmd_words(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        if not self.watch_words:
            await event.respond("Список слов пуст.")
            return
        text = "📋 <b>Слова для мониторинга:</b>\n" + "\n".join(
            f"• <code>{html_escape(word)}</code>" for word in self.watch_words
        )
        await event.respond(text, parse_mode="html")

    async def cmd_delword(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        parts = (event.message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await event.respond("Напишите так: <code>/delword слово</code>", parse_mode="html")
            return
        word = parts[1].strip().lower()
        if word in self.watch_words:
            self.watch_words.remove(word)
            await self.save_watch_words()
            await event.respond(f"Слово <code>{html_escape(word)}</code> удалено.", parse_mode="html")
        else:
            await event.respond("Такого слова в списке нет.", parse_mode="html")

    async def cmd_clearwords(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            await event.respond(
                f"Ваш ID: <code>{event.sender_id}</code>\n"
                "Доступ к поиску не выдан.",
                parse_mode="html",
            )
            return
        self.watch_words = []
        await self.save_watch_words()
        await event.respond("Список слов очищен.")

    async def on_group_message(self, event: events.NewMessage.Event) -> None:
        if event.out or event.is_private or not self.watch_words:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        matched = self.match_words(text)
        if not matched:
            return
        try:
            chat = await event.get_chat()
            sender = await event.get_sender()
            chat_name = getattr(chat, "title", None) or (fmt_user(chat) if chat else "неизвестно")
            sender_name = fmt_user(sender) if sender else str(event.sender_id)
            link = message_link(chat, event.message) if isinstance(chat, Channel) else None
            alert = (
                f"⚠️ <b>Слово из списка:</b> <code>{html_escape(matched)}</code>\n"
                f"💬 <b>Чат:</b> {html_escape(chat_name)}\n"
                f"👤 <b>Автор:</b> {html_escape(sender_name)} "
                f"(ID: <code>{event.sender_id}</code>)\n"
                f"🕐 <b>Время:</b> <code>{event.message.date.strftime('%d.%m.%Y %H:%M')}</code>\n\n"
                f"{html_escape(text[:400])}{'…' if len(text) > 400 else ''}"
            )
            if link:
                alert += f'\n\n<a href="{html_escape(link, quote=True)}">→ Перейти к сообщению</a>'
            for admin_id in self.owner_ids:
                if admin_id == self.scanner_id:
                    continue
                try:
                    await self.bot.send_message(admin_id, alert, parse_mode="html", link_preview=False)
                except Exception:
                    pass
        except Exception as error:
            logger.error("Ошибка мониторинга сообщения: %s", error)

    # ------------------------------------------------------------ оформление
    def format_results(self, result: dict, per_chat: int = 100) -> list[str]:
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

    def format_phrase_results(self, result: dict, per_chat: int = 100) -> list[str]:
        phrase = html_escape(result["phrase"])
        header = (
            f"🔍 <b>Результаты поиска по фразе:</b> <code>{phrase}</code>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Найдено сообщений: <b>{result['total_messages']}</b>\n"
            f"• Чатов с результатами: <b>{len(result['chats'])}</b>\n"
            f"• Проверено групп: <b>{result['checked']}</b>\n"
            f"• Пропущено/недоступно: <b>{result['skipped']}</b>\n\n"
        )
        blocks = []
        if result["notes"]:
            blocks.append("<b>Ограничения поиска:</b>\n" + "\n".join(
                "• " + html_escape(note) for note in result["notes"]
            ) + "\n\n")
        if not result["chats"]:
            blocks.append("Сообщения с такой фразой в открытых источниках не найдены.\n\n")
        else:
            blocks.append("🔓 <b>ОТКРЫТЫЕ ГРУППЫ/КАНАЛЫ:</b>\n")
            for chat in result["chats"][:15]:
                lines = [
                    f"<b>{html_escape(chat['name'])}</b>",
                    f"📝 Сообщений: {chat['count']} ({chat['source']})",
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
            if len(result["chats"]) > 15:
                blocks.append(f"Ещё {len(result['chats']) - 15} групп с результатами не показаны.\n\n")

        return self.split(header, blocks)

    @staticmethod
    def split(header: str, blocks: list[str], limit: int = 3600) -> list[str]:
        parts = []
        current = header

        def flush():
            nonlocal current
            if current.strip():
                parts.append(current)
                current = ""

        for block in blocks:
            if len(block) <= limit:
                if len(current) + len(block) > limit:
                    flush()
                current += block
                continue

            flush()
            for line in block.splitlines(keepends=True):
                if len(line) > limit:
                    flush()
                    for i in range(0, len(line), limit):
                        parts.append(line[i:i + limit])
                    continue
                if current.strip() and len(current) + len(line) > limit:
                    flush()
                current += line

        flush()
        return parts

    async def deep_phrase_search(self, phrase: str, status=None) -> dict:
        async def progress(text):
            if status:
                try:
                    await status.edit(text, parse_mode=None)
                except RPCError:
                    pass

        runner = PhraseSearchRunner(
            self.user, phrase, max_chats=MAX_CHATS, per_chat=MAX_PER_CHAT,
            progress=progress,
        )
        result = await runner.run()
        self.next_search_at = time.monotonic() + result["retry_after"]
        return result

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

    async def run_phrase_search(self, event: events.NewMessage.Event, phrase: str) -> None:
        if self.busy:
            await event.respond("⏳ Уже ищу, подождите.")
            return

        remaining = self.next_search_at - time.monotonic()
        if remaining > 0:
            await event.respond(
                f"Telegram временно ограничил запросы. Повторите через {int(remaining) + 1} с."
            )
            return
        if len(phrase) > 200:
            await event.respond("Фраза слишком длинная. Максимум 200 символов.")
            return

        self.busy = True
        status = None
        try:
            status = await event.respond(
                f"🔍 <b>Ищу по фразе:</b> <code>{html_escape(phrase)}</code>\n\n"
                "Сканирую открытые группы… ⏳",
                parse_mode="html",
            )

            result = await self.deep_phrase_search(phrase, status)
            parts = self.format_phrase_results(result)

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
            logger.error("Ошибка поиска по фразе: %s", type(error).__name__)
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
