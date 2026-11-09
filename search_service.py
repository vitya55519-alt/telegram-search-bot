import asyncio
import html
import logging
import re
import time
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from telethon import functions, types, utils
from telethon.errors import ChatAdminRequiredError, FloodWaitError, RPCError

logger = logging.getLogger(__name__)
PUBLIC_PATH = re.compile(r"/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/([0-9]+))?/?")
RESERVED = {"joinchat", "share", "proxy", "socks", "addstickers", "addemoji", "invoice"}


def public_references(text):
    found = {}
    for token in re.findall(r"https?://[^\s<>\"')\]]+", html.unescape(text), re.IGNORECASE):
        try:
            url = urlsplit(token)
            if url.netloc.lower() in {"duckduckgo.com", "www.duckduckgo.com", "lite.duckduckgo.com", "html.duckduckgo.com"}:
                target = parse_qs(url.query).get("uddg", [""])[0]
                url = urlsplit(target)
            if url.scheme.lower() not in {"http", "https"} or url.netloc.lower() != "t.me":
                continue
            match = PUBLIC_PATH.fullmatch(url.path)
            if not match or match[1].lower() in RESERVED:
                continue
            key = (match[1].lower(), int(match[2]) if match[2] else None)
            found.setdefault(key, key)
        except ValueError:
            continue
    return list(found.values())


def indexed_references(username):
    query = quote(f'"@{username}" site:t.me', safe="")
    url = "https://r.jina.ai/https://lite.duckduckgo.com/lite/?q=" + query
    request = Request(url, headers={"User-Agent": "TelegramSearchBot/1.0"})
    with urlopen(request, timeout=20) as response:
        body = response.read(1_000_000).decode("utf-8", errors="replace")
    return public_references(body)[:40]


def message_link(entity, message):
    if not isinstance(entity, types.Channel):
        return None
    if entity.username:
        return f"https://t.me/{entity.username}/{message.id}"
    return f"https://t.me/c/{entity.id}/{message.id}"


def is_authored(message, user_id):
    return (
        isinstance(message.from_id, types.PeerUser)
        and message.from_id.user_id == user_id
        and message.fwd_from is None
    )


def is_mention(message, user):
    if any(
        isinstance(entity, types.MessageEntityMentionName) and entity.user_id == user.id
        for entity in (message.entities or [])
    ):
        return True
    return bool(user.username and re.search(
        rf"(?<!\w)@{re.escape(user.username)}(?!\w)",
        message.message or "", re.IGNORECASE,
    ))


class SearchRunner:
    def __init__(self, client, user, max_chats=300, per_chat=300, progress=None):
        self.client = client
        self.user = user
        self.max_chats = max_chats
        self.per_chat = per_chat
        self.progress = progress
        self.chats = {}
        self.joined = set()
        self.mentions = {}
        self.notes = []
        self.checked = 0
        self.external_checked = 0
        self.skipped = 0
        self.retry_after = 0
        self.last_progress = 0

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    async def notify(self, text):
        if not self.progress or time.monotonic() - self.last_progress < 4:
            return
        self.last_progress = time.monotonic()
        await self.progress(text)

    async def request(self, request, label):
        try:
            return await asyncio.wait_for(self.client(request), timeout=25)
        except FloodWaitError as error:
            self.retry_after = max(self.retry_after, error.seconds)
            self.note(f"Telegram остановил запросы на {error.seconds} с.")
            return None
        except (RPCError, asyncio.TimeoutError, ValueError) as error:
            logger.info("%s: %s", label, type(error).__name__)
            self.note(f"{label}: источник недоступен, охват неполный.")
            return None

    def add_chat(self, entity, source, known=False):
        if not isinstance(entity, (types.Channel, types.Chat)):
            return None
        key = utils.get_peer_id(entity)
        if known and not getattr(entity, "left", False):
            self.joined.add(key)
        is_joined = key in self.joined
        if key in self.chats:
            return self.chats[key]
        if is_joined:
            if len(self.joined) > self.max_chats:
                self.note("Достигнут предел ваших групп.")
                return None
        else:
            external_count = len(self.chats) - len(self.joined)
            if external_count >= self.max_chats:
                self.note("Достигнут предел найденных источников вне подписок.")
                return None
        if len(self.chats) >= self.max_chats * 2:
            self.note("Достигнут общий предел источников.")
            return None
        public = isinstance(entity, types.Channel) and bool(entity.username)
        if not public and not is_joined:
            return None
        chat = {
            "entity": entity, "name": entity.title, "public": public,
            "source": source, "messages": {}, "limited": False,
        }
        self.chats[key] = chat
        return chat

    def remember(self, chat, message):
        if not isinstance(message, types.Message):
            return
        key = (utils.get_peer_id(chat["entity"]), message.id)
        item = {
            "id": message.id, "date": message.date,
            "text": message.message or "[медиа/стикер/файл]",
            "link": message_link(chat["entity"], message), "chat": chat["name"],
        }
        if is_authored(message, self.user.id):
            if len(chat["messages"]) < self.per_chat or message.id in chat["messages"]:
                chat["messages"][message.id] = item
            else:
                chat["limited"] = True
            self.mentions.pop(key, None)
        elif is_mention(message, self.user):
            self.mentions[key] = item

    async def discover(self):
        await self.notify("Ищу открытые группы и обсуждения вне подписок…")
        try:
            async for dialog in self.client.iter_dialogs():
                self.add_chat(dialog.entity, "dialogs", known=True)
        except FloodWaitError as error:
            self.retry_after = max(self.retry_after, error.seconds)
            self.note(f"Telegram остановил запросы на {error.seconds} с.")
        except RPCError as error:
            logger.info("Диалоги: %s", type(error).__name__)
            self.note("Не удалось полностью прочитать список ваших групп.")

        if self.user.username:
            await self.public_posts()
            await self.web_sources()
            response = await self.request(
                functions.contacts.SearchRequest(q=self.user.username, limit=100),
                "Поиск открытых групп",
            )
            if response:
                for entity in response.chats:
                    self.add_chat(entity, "public_search")
        else:
            self.note("У пользователя нет ника: поиск упоминаний в интернете недоступен.")

        full = await self.request(
            functions.users.GetFullUserRequest(id=self.user), "Открытый канал профиля"
        )
        if full:
            channel_id = getattr(full.full_user, "personal_channel_id", None)
            for entity in full.chats:
                if entity.id == channel_id:
                    self.add_chat(entity, "profile")

        seeds = [chat["entity"] for chat in self.chats.values()
                 if chat["public"] and getattr(chat["entity"], "broadcast", False)]
        for entity in seeds[:3]:
            response = await self.request(
                functions.channels.GetChannelRecommendationsRequest(channel=entity),
                "Похожие открытые каналы",
            )
            if response:
                for channel in response.chats:
                    self.add_chat(channel, "recommendations")

        for chat in list(self.chats.values()):
            entity = chat["entity"]
            if not isinstance(entity, types.Channel) or not entity.broadcast:
                continue
            await self.notify(f"Ищу обсуждения каналов. Источников: {len(self.chats)}…")
            full = await self.request(
                functions.channels.GetFullChannelRequest(channel=entity),
                "Обсуждения каналов",
            )
            if full:
                linked_id = full.full_chat.linked_chat_id
                for linked in full.chats:
                    if linked.id == linked_id:
                        self.add_chat(linked, "discussion")

    async def public_posts(self):
        query = f"@{self.user.username}"
        quota = await self.request(
            functions.channels.CheckSearchPostsFloodRequest(query=query),
            "Поиск публикаций Telegram",
        )
        if quota is None:
            return
        if not quota.query_is_free and quota.remains <= 0:
            self.note("Бесплатный поиск публикаций Telegram исчерпан; платные запросы отключены.")
            return
        offset_rate, offset_peer, offset_id = 0, types.InputPeerEmpty(), 0
        seen_cursors = set()
        for page in range(5):
            response = await self.request(
                functions.channels.SearchPostsRequest(
                    query=query, offset_rate=offset_rate, offset_peer=offset_peer,
                    offset_id=offset_id, limit=100, allow_paid_stars=None,
                ), "Поиск публикаций Telegram",
            )
            if response is None:
                break
            entities = {utils.get_peer_id(e): e for e in response.chats}
            for entity in response.chats:
                self.add_chat(entity, "public_posts")
            messages = [m for m in response.messages if isinstance(m, types.Message)]
            for message in messages:
                chat = self.chats.get(utils.get_peer_id(message.peer_id))
                if chat:
                    self.remember(chat, message)
            if not messages:
                break
            last = messages[-1]
            peer_id = utils.get_peer_id(last.peer_id)
            entity = entities.get(peer_id)
            if entity is None:
                self.note("Не удалось продолжить список открытых публикаций.")
                break
            next_rate = getattr(response, "next_rate", None)
            offset_rate = next_rate if next_rate is not None else int(last.date.timestamp())
            cursor = (offset_rate, peer_id, last.id)
            if cursor in seen_cursors:
                self.note("Telegram повторил страницу публикаций; повторный обход остановлен.")
                break
            seen_cursors.add(cursor)
            offset_peer = utils.get_input_peer(entity)
            offset_id = last.id
            if page == 4:
                self.note("Проверены первые 500 открытых публикаций с упоминанием ника.")

    async def web_sources(self):
        try:
            references = await asyncio.wait_for(
                asyncio.to_thread(indexed_references, self.user.username), timeout=22,
            )
        except (URLError, OSError, asyncio.TimeoutError, ValueError):
            self.note("Интернет-поиск недоступен; поиск через Telegram продолжается.")
            return
        resolved = {}
        for username, message_id in references:
            if self.retry_after:
                break
            if username not in resolved:
                try:
                    resolved[username] = await asyncio.wait_for(
                        self.client.get_entity("@" + username), timeout=15,
                    )
                except FloodWaitError as error:
                    self.retry_after = max(self.retry_after, error.seconds)
                    self.note(f"Telegram остановил запросы на {error.seconds} с.")
                    break
                except (RPCError, ValueError, asyncio.TimeoutError):
                    resolved[username] = None
            entity = resolved[username]
            chat = self.add_chat(entity, "web")
            if not chat or message_id is None:
                continue
            try:
                message = await asyncio.wait_for(
                    self.client.get_messages(entity, ids=message_id), timeout=15,
                )
                self.remember(chat, message)
            except FloodWaitError as error:
                self.retry_after = max(self.retry_after, error.seconds)
                self.note(f"Telegram остановил запросы на {error.seconds} с.")
                break
            except (RPCError, ValueError, asyncio.TimeoutError):
                self.note("Часть публикаций из интернет-поиска недоступна в Telegram.")

    async def scan_chat(self, chat):
        count = 0
        try:
            async for message in self.client.iter_messages(
                chat["entity"], from_user=self.user, limit=self.per_chat + 1,
            ):
                count += 1
                self.remember(chat, message)
        except ChatAdminRequiredError:
            # В некоторых публичных каналах поиск по автору доступен только админам;
            # читаем последние сообщения и фильтруем вручную.
            async for message in self.client.iter_messages(
                chat["entity"], limit=self.per_chat * 3,
            ):
                if is_authored(message, self.user.id) or is_mention(message, self.user):
                    count += 1
                    self.remember(chat, message)
        if count > self.per_chat:
            chat["limited"] = True

    def result(self):
        found = []
        for key, chat in self.chats.items():
            if not chat["messages"]:
                continue
            found.append({
                "name": chat["name"], "public": chat["public"],
                "source": "dialogs" if key in self.joined else chat["source"],
                "messages": sorted(chat["messages"].values(), key=lambda m: m["date"], reverse=True),
                "count": len(chat["messages"]), "limited": chat["limited"],
            })
        if any(chat["limited"] for chat in self.chats.values()):
            self.note(f"В отдельных группах достигнут предел {self.per_chat} сообщений; показаны не все.")
        return {
            "user": self.user, "chats": found,
            "total_messages": sum(chat["count"] for chat in found),
            "public": [chat for chat in found if chat["public"]],
            "private": [chat for chat in found if not chat["public"]],
            "discovered": [chat for chat in found if chat["source"] != "dialogs"],
            "external_found": len(set(self.chats) - self.joined),
            "checked": self.checked, "external_checked": self.external_checked,
            "skipped": self.skipped, "mentions": list(self.mentions.values()),
            "notes": list(self.notes), "retry_after": self.retry_after,
        }

    async def run(self):
        try:
            async with asyncio.timeout(600):
                try:
                    async with asyncio.timeout(180):
                        await self.discover()
                except asyncio.TimeoutError:
                    self.note("Время обнаружения источников истекло; проверяю уже найденные.")
                external = [(k, c) for k, c in self.chats.items() if k not in self.joined]
                joined = [(k, c) for k, c in self.chats.items() if k in self.joined]
                items = []
                for index in range(max(len(external), len(joined))):
                    if index < len(external):
                        items.append(external[index])
                    if index < len(joined):
                        items.append(joined[index])
                if len(items) > self.max_chats:
                    self.note(f"За один поиск проверяется не более {self.max_chats} групп; охват неполный.")
                for index, (key, chat) in enumerate(items[:self.max_chats], 1):
                    await self.notify(
                        f"Проверяю группы: {index}/{min(len(items), self.max_chats)}. "
                        f"Вне подписок проверено: {self.external_checked}."
                    )
                    try:
                        await asyncio.wait_for(self.scan_chat(chat), timeout=30)
                        self.checked += 1
                        self.external_checked += int(key not in self.joined)
                    except FloodWaitError as error:
                        self.retry_after = max(self.retry_after, error.seconds)
                        self.note(f"Telegram остановил запросы на {error.seconds} с. Показана уже найденная часть.")
                        break
                    except (RPCError, ValueError, asyncio.TimeoutError) as error:
                        self.skipped += 1
                        logger.info("Проверка группы: %s", type(error).__name__)
                    await asyncio.sleep(0.2)
        except asyncio.TimeoutError:
            self.note("Достигнут предел 10 минут; показана уже найденная часть.")
        if self.skipped:
            self.note(f"Не удалось проверить групп: {self.skipped}.")
        return self.result()
