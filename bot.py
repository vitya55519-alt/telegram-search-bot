#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот глубокого поиска комментариев пользователя.

Схема работы:
  * бот принимает запрос от администратора и присылает отчёт;
  * поиск выполняет авторизованный аккаунт (сессия user_session),
    поэтому видны все его чаты, включая архив.
"""

import asyncio
import logging
import os
from html import escape as html_escape
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UserNotParticipantError,
)
from telethon.tl.types import Channel, User

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
USER_SESSION = os.getenv("USER_SESSION", "user_session")
BOT_SESSION = os.getenv("BOT_SESSION", "bot_session")
MAX_PER_CHAT = int(os.getenv("MAX_PER_CHAT", "300"))
MAX_CHATS = int(os.getenv("MAX_CHATS", "300"))

if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_ID]):
    raise ValueError("Заполните API_ID, API_HASH, BOT_TOKEN и ADMIN_ID!")

HELP_TEXT = (
    "🔍 <b>Глубокий поиск комментариев</b>\n\n"
    "Отправьте одно из значений:\n"
    "• <code>@nickname</code> — по нику\n"
    "• <code>123456789</code> — по номеру пользователя\n"
    "• <code>+7 999 123-45-67</code> — по телефону\n\n"
    "Поиск идёт по всем чатам аккаунта-сканера, включая архив. "
    "В отчёте: сколько сообщений найдено, в скольких чатах, "
    "публичные и приватные, а также новые чаты, найденные через общие "
    "чаты и глобальный поиск.\n\n"
    "Команды: /search, /find или просто пришлите ник."
)


def fmt_user(user: User) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    return name or str(user.id)


def msg_link(entity, message) -> Optional[str]:
    if not isinstance(entity, Channel):
        return None
    if getattr(entity, "username", None):
        return f"https://t.me/{entity.username}/{message.id}"
    return f"https://t.me/c/{entity.id}/{message.id}"


class DeepSearchBot:
    def __init__(self) -> None:
        self.user = TelegramClient(USER_SESSION, API_ID, API_HASH)
        self.bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
        self.busy = False

    # ---------------------------------------------------------------- запуск
    async def start(self) -> None:
        await self.user.connect()
        if not await self.user.is_user_authorized():
            raise RuntimeError(
                "Сессия аккаунта-сканера не авторизована. "
                "Обновите файл user_session.session."
            )

        me = await self.user.get_me()
        logger.info("Сканер: %s (ID: %s)", fmt_user(me), me.id)

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
        if not self.is_admin(event):
            return
        chats = await self.count_chats()
        await event.respond(
            f"👋 <b>Привет!</b>\n\n"
            f"Сканирую через аккаунт @{await self.scanner_nick()}: "
            f"вижу <b>{chats}</b> чатов и групп, включая архив.\n\n"
            f"{HELP_TEXT}",
            parse_mode="html",
        )

    async def cmd_help(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            return
        await event.respond(HELP_TEXT, parse_mode="html")

    async def cmd_search(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            return
        parts = (event.message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await event.respond(
                "Напишите так: <code>/search @nickname</code>", parse_mode="html"
            )
            return
        await self.run_search(event, parts[1].strip())

    async def cmd_any(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event):
            return
        text = (event.message.text or "").strip()
        if not text or text.startswith("/"):
            return
        await self.run_search(event, text)

    @staticmethod
    def is_admin(event: events.NewMessage.Event) -> bool:
        return event.sender_id == ADMIN_ID

    # --------------------------------------------------------------- сервис
    async def scanner_nick(self) -> str:
        me = await self.user.get_me()
        return me.username or me.first_name or str(me.id)

    async def count_chats(self) -> int:
        total = 0
        async for dialog in self.user.iter_dialogs():
            if not dialog.is_user:
                total += 1
        return total

    async def resolve_user(self, query: str) -> Optional[User]:
        try:
            if query.startswith("+"):
                digits = "".join(ch for ch in query if ch.isdigit())
                contacts = await self.user(functions.contacts.GetContactsRequest(hash=0))
                for candidate in contacts.users:
                    phone = getattr(candidate, "phone", None)
                    if phone and "".join(ch for ch in phone if ch.isdigit()) == digits:
                        return candidate
                return None

            if query.isdigit():
                try:
                    entity = await self.user.get_entity(int(query))
                except Exception:
                    return None
                return entity if isinstance(entity, User) else None

            if query.startswith("@"):
                try:
                    entity = await self.user.get_entity(query)
                except Exception:
                    return None
                return entity if isinstance(entity, User) else None

            # ник без @ или имя
            try:
                entity = await self.user.get_entity(query)
                if isinstance(entity, User):
                    return entity
            except Exception:
                pass

            found = await self.user(functions.contacts.SearchRequest(q=query, limit=10))
            return found.users[0] if found.users else None
        except Exception as error:
            logger.warning("Не удалось найти %s: %s", query, error)
            return None

    async def collect_chats(self, user: User) -> dict:
        chats: dict[int, dict] = {}

        try:
            async for dialog in self.user.iter_dialogs():
                entity = dialog.entity
                if dialog.is_user or entity.id in chats:
                    continue
                chats[entity.id] = {
                    "entity": entity,
                    "name": dialog.name or getattr(entity, "title", str(entity.id)),
                    "public": bool(getattr(entity, "username", None)),
                    "source": "dialogs",
                }
        except Exception as error:
            logger.error("Ошибка чтения диалогов: %s", error)

        # общие чаты: там, где состоят и сканер, и искомый пользователь
        try:
            common = await self.user(
                functions.messages.GetCommonChatsRequest(
                    user_id=user.id, max_id=0, limit=100
                )
            )
            for entity in common.chats:
                if entity.id in chats:
                    continue
                chats[entity.id] = {
                    "entity": entity,
                    "name": getattr(entity, "title", str(entity.id)),
                    "public": bool(getattr(entity, "username", None)),
                    "source": "common",
                }
        except Exception as error:
            logger.debug("Общие чаты недоступны: %s", error)

        # глобальный поиск упоминаний ника
        if getattr(user, "username", None):
            try:
                result = await self.user(
                    functions.messages.SearchGlobalRequest(
                        q=f"@{user.username}",
                        filter=types.InputMessagesFilterEmpty(),
                        min_date=None,
                        max_date=None,
                        offset_rate=0,
                        offset_peer=types.InputPeerEmpty(),
                        offset_id=0,
                        limit=100,
                    )
                )
                for entity in result.chats:
                    if entity.id in chats:
                        continue
                    chats[entity.id] = {
                        "entity": entity,
                        "name": getattr(entity, "title", str(entity.id)),
                        "public": bool(getattr(entity, "username", None)),
                        "source": "global",
                    }
            except Exception as error:
                logger.debug("Глобальный поиск недоступен: %s", error)

        return chats

    async def deep_search(self, user: User, status=None) -> dict:
        chats = await self.collect_chats(user)
        logger.info("Чатов к проверке: %s", len(chats))

        result = {
            "user": user,
            "chats": [],
            "total_messages": 0,
            "public": [],
            "private": [],
            "discovered": [],
        }

        items = list(chats.values())[:MAX_CHATS]
        for index, chat in enumerate(items, 1):
            entity = chat["entity"]

            try:
                messages = await asyncio.wait_for(
                    self.scan_chat(entity, user), timeout=90
                )
            except asyncio.TimeoutError:
                logger.debug("Пропуск по времени: %s", chat["name"])
                continue
            except FloodWaitError as error:
                wait = min(error.seconds, 60)
                logger.warning("Лимит Telegram, пауза %s с", wait)
                await asyncio.sleep(wait)
                continue
            except (ChannelPrivateError, UserNotParticipantError):
                continue
            except Exception as error:
                logger.debug("Ошибка в %s: %s", chat["name"], error)
                continue

            if messages:
                info = {
                    "name": chat["name"],
                    "messages": messages,
                    "count": len(messages),
                    "public": chat["public"],
                    "source": chat["source"],
                }
                result["chats"].append(info)
                result["public" if chat["public"] else "private"].append(info)
                if chat["source"] != "dialogs":
                    result["discovered"].append(info)
                result["total_messages"] += len(messages)
                logger.info("%s: %s сообщений", chat["name"], len(messages))

            if status and index % 5 == 0:
                try:
                    await status.edit(
                        f"🔎 Проверено чатов: <b>{index}</b> из <b>{len(items)}</b>\n"
                        f"Найдено сообщений: <b>{result['total_messages']}</b>",
                        parse_mode="html",
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.15)

        return result

    async def scan_chat(self, entity, user: User) -> list:
        messages = []
        async for message in self.user.iter_messages(
            entity, from_user=user.id, limit=MAX_PER_CHAT
        ):
            messages.append(
                {
                    "id": message.id,
                    "date": message.date,
                    "text": message.text or "[медиа/стикер/файл]",
                    "link": msg_link(entity, message),
                }
            )
        return messages

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
            f"• Обнаружено новых: <b>{len(result['discovered'])}</b>\n\n"
            f"{'─' * 40}\n\n"
        )

        if not result["chats"]:
            return [header + "❌ Сообщения не найдены."]

        blocks = []
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

        self.busy = True
        status = None
        try:
            status = await event.respond(
                f"🔍 <b>Запускаю глубокий поиск…</b>\n\n"
                f"Запрос: <code>{html_escape(query)}</code>",
                parse_mode="html",
            )

            user = await self.resolve_user(query)
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

            await event.respond(
                "✅ <b>Поиск завершён!</b>\n\n"
                f"Найдено <b>{result['total_messages']}</b> сообщений "
                f"в <b>{len(result['chats'])}</b> чатах.",
                parse_mode="html",
            )
        except Exception as error:
            logger.exception("Ошибка поиска")
            text = f"❌ Ошибка поиска: <code>{html_escape(str(error))}</code>"
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
