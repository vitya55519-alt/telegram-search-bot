#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot для глубокого поиска комментариев пользователя в группах и чатах
Максимальный охват: публичные и приватные группы, каналы, супергруппы
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Union, List
from dotenv import load_dotenv

from telethon import TelegramClient, events, types, functions
from telethon.tl.types import User, Channel, Chat, MessageEntityMention, MessageEntityTextUrl
from telethon.errors import FloodWaitError, ChannelPrivateError, UserNotParticipantError

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# Проверка обязательных параметров
if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_ID]):
    raise ValueError("Заполните все необходимые параметры в .env файле!")


class TelegramSearchBot:
    def __init__(self):
        self.client = TelegramClient('bot_session', API_ID, API_HASH)
        self.bot_token = BOT_TOKEN
        self.admin_id = ADMIN_ID
        
    async def start(self):
        """Запуск бота"""
        await self.client.start(bot_token=self.bot_token)
        logger.info("🚀 Бот успешно запущен с расширенным поиском!")
        
        # Регистрация обработчиков
        self.client.add_event_handler(self.handle_start, events.NewMessage(pattern='/start'))
        self.client.add_event_handler(self.handle_help, events.NewMessage(pattern='/help'))
        
        # Автоматический поиск без команд - обрабатываем любой текст
        self.client.add_event_handler(self.handle_auto_search, events.NewMessage(incoming=True))
        
        # Ожидание событий
        await self.client.run_until_disconnected()
    
    def is_admin(self, user_id: int) -> bool:
        """Проверка, является ли пользователь администратором"""
        return user_id == self.admin_id
    
    async def handle_start(self, event):
        """Обработчик команды /start"""
        await event.respond(
            "👋 **Привет! Я бот для глубокого поиска пользователей в Telegram**\n\n"
            "🔥 **Что я умею:**\n"
            "• Поиск в ВСЕХ доступных группах и каналах\n"
            "• Публичные И приватные чаты\n"
            "• Глобальный поиск по Telegram\n"
            "• Автоматическое обнаружение новых групп\n\n"
            "⚡ **Просто отправь мне:**\n"
            "• `@username` - username пользователя\n"
            "• `+79991234567` - номер телефона\n"
            "• `123456789` - user ID\n\n"
            "Никаких команд не нужно! Просто напиши то, что хочешь найти.\n\n"
            "💡 Для справки: `/help`"
        )
        raise events.StopPropagation
    
    async def handle_help(self, event):
        """Обработчик команды /help"""
        await event.respond(
            "📖 **Расширенная справка**\n\n"
            "**Форматы поиска:**\n"
            "Просто отправь мне один из вариантов:\n"
            "• `@username` - по имени пользователя\n"
            "• `+79991234567` - по номеру телефона\n"
            "• `123456789` - по User ID\n"
            "• `Pavel Durov` - по имени (попытка найти)\n\n"
            "**Примеры:**\n"
            "`@durov`\n"
            "`+79991234567`\n"
            "`12345678`\n\n"
            "**🔍 Где я ищу:**\n"
            "1️⃣ Все ваши диалоги и группы\n"
            "2️⃣ Публичные каналы и супергруппы\n"
            "3️⃣ Глобальный поиск Telegram\n"
            "4️⃣ Связанные и рекомендуемые чаты\n"
            "5️⃣ Истории пересылок\n"
            "6️⃣ Упоминания в других чатах\n\n"
            "**⚠️ Ограничения:**\n"
            "• Приватные чаты: только где вы состоите\n"
            "• Публичные чаты: без ограничений\n"
            "• Поиск может занять 1-3 минуты\n"
            "• Telegram API имеет rate limits\n\n"
            "💡 **Совет:** Добавляйте бота в больше групп для лучших результатов!"
        )
        raise events.StopPropagation
    
    async def handle_auto_search(self, event):
        """Автоматический поиск без команд - обработка любого текста"""
        # Пропускаем команды
        if event.message.text.startswith('/'):
            return
        
        # Только для администратора
        if not self.is_admin(event.sender_id):
            await event.respond("❌ Этот бот доступен только администратору.")
            raise events.StopPropagation
        
        query = event.message.text.strip()
        
        if not query:
            return
        
        await self.process_search(event, query)
        raise events.StopPropagation
    
    async def resolve_user(self, query: str) -> Optional[User]:
        """
        Определение пользователя по username, телефону, ID или имени
        
        Args:
            query: username (@username), телефон (+79991234567), ID (123456789) или имя
            
        Returns:
            User объект или None
        """
        try:
            # Поиск по username
            if query.startswith('@'):
                user = await self.client.get_entity(query)
                return user if isinstance(user, User) else None
            
            # Поиск по телефону
            elif query.startswith('+'):
                # Попытка найти через контакты
                try:
                    contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
                    phone_clean = query.replace('+', '').replace(' ', '').replace('-', '')
                    for user in contacts.users:
                        if hasattr(user, 'phone') and user.phone:
                            user_phone = user.phone.replace(' ', '').replace('-', '')
                            if user_phone == phone_clean:
                                return user
                except Exception as e:
                    logger.debug(f"Поиск по телефону через контакты не удался: {e}")
                
                # Попытка напрямую
                try:
                    user = await self.client.get_entity(query)
                    return user if isinstance(user, User) else None
                except:
                    pass
                
                return None
            
            # Поиск по ID
            elif query.isdigit():
                user_id = int(query)
                user = await self.client.get_entity(user_id)
                return user if isinstance(user, User) else None
            
            # Поиск по имени через глобальный поиск
            else:
                try:
                    result = await self.client(functions.contacts.SearchRequest(
                        q=query,
                        limit=10
                    ))
                    
                    if result.users:
                        # Возвращаем первого найденного пользователя
                        return result.users[0]
                except Exception as e:
                    logger.debug(f"Глобальный поиск по имени не дал результатов: {e}")
                
                return None
                
        except ValueError:
            logger.error(f"Неверный формат запроса: {query}")
            return None
        except Exception as e:
            logger.error(f"Ошибка при поиске пользователя {query}: {e}")
            return None
    
    async def search_in_global_search(self, user: User, limit: int = 50) -> List[dict]:
        """
        Глобальный поиск по Telegram с использованием username пользователя
        """
        found_chats = []
        
        if not user.username:
            return found_chats
        
        try:
            logger.info(f"🌐 Глобальный поиск упоминаний @{user.username}...")
            
            # Поиск упоминаний username в глобальном поиске
            result = await self.client(functions.messages.SearchGlobalRequest(
                q=f"@{user.username}",
                offset_rate=0,
                offset_peer=types.InputPeerEmpty(),
                offset_id=0,
                limit=limit
            ))
            
            # Обработка найденных чатов
            chat_ids = set()
            for message in result.messages:
                if hasattr(message, 'peer_id'):
                    chat_id = None
                    if isinstance(message.peer_id, types.PeerChannel):
                        chat_id = message.peer_id.channel_id
                    elif isinstance(message.peer_id, types.PeerChat):
                        chat_id = message.peer_id.chat_id
                    
                    if chat_id and chat_id not in chat_ids:
                        chat_ids.add(chat_id)
                        
                        try:
                            chat = await self.client.get_entity(message.peer_id)
                            if isinstance(chat, (Channel, Chat)):
                                found_chats.append({
                                    'entity': chat,
                                    'name': getattr(chat, 'title', 'Unknown'),
                                    'id': chat_id,
                                    'source': 'global_search'
                                })
                        except Exception as e:
                            logger.debug(f"Не удалось получить чат {chat_id}: {e}")
            
            logger.info(f"✅ Найдено {len(found_chats)} чатов через глобальный поиск")
            
        except Exception as e:
            logger.debug(f"Глобальный поиск не дал результатов: {e}")
        
        return found_chats
    
    async def discover_public_channels(self, user: User, limit: int = 100) -> List[dict]:
        """
        Обнаружение публичных каналов и групп через различные методы
        """
        discovered = []
        
        try:
            # Метод 1: Рекомендованные чаты
            try:
                logger.info("📡 Поиск в рекомендованных чатах...")
                result = await self.client(functions.channels.GetChannelRecommendationsRequest(
                    channel=types.InputChannelEmpty()
                ))
                
                for chat in result.chats:
                    if isinstance(chat, Channel):
                        discovered.append({
                            'entity': chat,
                            'name': chat.title,
                            'id': chat.id,
                            'source': 'recommendations'
                        })
            except Exception as e:
                logger.debug(f"Рекомендации не получены: {e}")
            
            # Метод 2: Популярные каналы через поиск
            try:
                logger.info("🔥 Поиск популярных каналов...")
                common_topics = ['news', 'tech', 'crypto', 'мемы', 'новости']
                
                for topic in common_topics:
                    try:
                        result = await self.client(functions.contacts.SearchRequest(
                            q=topic,
                            limit=20
                        ))
                        
                        for chat in result.chats:
                            if isinstance(chat, Channel) and chat.id not in [d['id'] for d in discovered]:
                                discovered.append({
                                    'entity': chat,
                                    'name': chat.title,
                                    'id': chat.id,
                                    'source': 'popular_search'
                                })
                        
                        await asyncio.sleep(1)  # Rate limiting
                    except FloodWaitError as e:
                        logger.warning(f"FloodWait: ждем {e.seconds} секунд")
                        await asyncio.sleep(e.seconds)
                    except Exception:
                        pass
                        
            except Exception as e:
                logger.debug(f"Поиск популярных каналов не удался: {e}")
            
        except Exception as e:
            logger.debug(f"Обнаружение публичных каналов завершилось с ошибкой: {e}")
        
        logger.info(f"✅ Обнаружено {len(discovered)} новых чатов")
        return discovered
    
    async def deep_search_user_messages(self, user: User, limit_per_chat: int = 100) -> dict:
        """
        ГЛУБОКИЙ поиск всех сообщений пользователя везде, где только можно
        
        Args:
            user: Объект пользователя
            limit_per_chat: Максимум сообщений на чат
            
        Returns:
            Словарь с полными результатами поиска
        """
        results = {
            'user': user,
            'chats': [],
            'total_messages': 0,
            'private_chats': [],
            'public_chats': [],
            'discovered_chats': []
        }
        
        all_chats_to_search = []
        processed_ids = set()
        
        # === ФАЗА 1: Диалоги пользователя ===
        logger.info("🔍 ФАЗА 1: Поиск в ваших диалогах...")
        try:
            dialogs = await self.client.get_dialogs(limit=None)
            
            for dialog in dialogs:
                if isinstance(dialog.entity, User):
                    continue
                
                if dialog.entity.id not in processed_ids:
                    is_public = hasattr(dialog.entity, 'username') and dialog.entity.username
                    all_chats_to_search.append({
                        'entity': dialog.entity,
                        'name': dialog.name,
                        'id': dialog.entity.id,
                        'public': is_public,
                        'source': 'dialogs'
                    })
                    processed_ids.add(dialog.entity.id)
            
            logger.info(f"✅ Найдено {len(all_chats_to_search)} диалогов")
        except Exception as e:
            logger.error(f"Ошибка при получении диалогов: {e}")
        
        # === ФАЗА 2: Глобальный поиск ===
        logger.info("🔍 ФАЗА 2: Глобальный поиск по Telegram...")
        try:
            global_chats = await self.search_in_global_search(user, limit=50)
            for chat_data in global_chats:
                if chat_data['id'] not in processed_ids:
                    all_chats_to_search.append(chat_data)
                    processed_ids.add(chat_data['id'])
        except Exception as e:
            logger.debug(f"Глобальный поиск завершился с ошибкой: {e}")
        
        # === ФАЗА 3: Обнаружение публичных каналов ===
        logger.info("🔍 ФАЗА 3: Обнаружение публичных каналов...")
        try:
            discovered = await self.discover_public_channels(user, limit=100)
            for chat_data in discovered:
                if chat_data['id'] not in processed_ids:
                    all_chats_to_search.append(chat_data)
                    processed_ids.add(chat_data['id'])
        except Exception as e:
            logger.debug(f"Обнаружение каналов завершилось с ошибкой: {e}")
        
        # === ПОИСК СООБЩЕНИЙ ВО ВСЕХ НАЙДЕННЫХ ЧАТАХ ===
        logger.info(f"🔍 Начинаю поиск сообщений в {len(all_chats_to_search)} чатах...")
        
        for idx, chat_data in enumerate(all_chats_to_search, 1):
            try:
                if idx % 10 == 0:
                    logger.info(f"Прогресс: {idx}/{len(all_chats_to_search)} чатов обработано...")
                
                messages = []
                entity = chat_data['entity']
                chat_name = chat_data['name']
                is_public = chat_data.get('public', False)
                
                # Поиск сообщений пользователя
                try:
                    async for message in self.client.iter_messages(
                        entity,
                        from_user=user.id,
                        limit=limit_per_chat
                    ):
                        link = None
                        if isinstance(entity, Channel):
                            if is_public and hasattr(entity, 'username') and entity.username:
                                link = f"https://t.me/{entity.username}/{message.id}"
                            else:
                                link = f"https://t.me/c/{str(entity.id)[4:]}/{message.id}"
                        
                        messages.append({
                            'id': message.id,
                            'date': message.date,
                            'text': message.text or '[Медиа/Стикер/Файл]',
                            'link': link,
                            'chat_id': entity.id
                        })
                
                except (ChannelPrivateError, UserNotParticipantError):
                    logger.debug(f"Нет доступа к чату: {chat_name}")
                    continue
                except FloodWaitError as e:
                    logger.warning(f"FloodWait: ждем {e.seconds} секунд")
                    await asyncio.sleep(e.seconds)
                    continue
                except Exception as e:
                    logger.debug(f"Ошибка поиска в {chat_name}: {e}")
                    continue
                
                if messages:
                    chat_info = {
                        'name': chat_name,
                        'id': entity.id,
                        'messages': messages,
                        'count': len(messages),
                        'public': is_public,
                        'source': chat_data.get('source', 'unknown')
                    }
                    
                    results['chats'].append(chat_info)
                    
                    if is_public:
                        results['public_chats'].append(chat_info)
                    else:
                        results['private_chats'].append(chat_info)
                    
                    if chat_data.get('source') in ['global_search', 'recommendations', 'popular_search']:
                        results['discovered_chats'].append(chat_info)
                    
                    results['total_messages'] += len(messages)
                    logger.info(f"✅ {chat_name}: найдено {len(messages)} сообщений")
                
                # Rate limiting
                await asyncio.sleep(0.5)
                    
            except Exception as e:
                logger.debug(f"Ошибка при обработке чата: {e}")
                continue
        
        logger.info(f"🎉 ПОИСК ЗАВЕРШЕН! Найдено {results['total_messages']} сообщений в {len(results['chats'])} чатах")
        return results
    
    async def format_search_results(self, results: dict, max_messages_per_chat: int = 3) -> List[str]:
        """
        Форматирование результатов поиска (может быть несколько сообщений)
        
        Returns:
            Список строк (если результат большой, разбиваем на части)
        """
        user = results['user']
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        user_id = user.id
        
        parts = []
        current_part = []
        
        # Заголовок
        header = [
            f"🔍 **Результаты глубокого поиска для {user_name}**",
            f"👤 User ID: `{user_id}`\n",
            f"📊 **Статистика:**",
            f"• Всего сообщений: **{results['total_messages']}**",
            f"• Всего чатов: **{len(results['chats'])}**",
            f"• Публичных: **{len(results['public_chats'])}**",
            f"• Приватных: **{len(results['private_chats'])}**",
            f"• Обнаружено новых: **{len(results['discovered_chats'])}**\n",
            "─" * 50 + "\n"
        ]
        
        current_part.extend(header)
        
        if not results['chats']:
            current_part.append("❌ Сообщения не найдены.")
            parts.append("\n".join(current_part))
            return parts
        
        # Публичные чаты
        if results['public_chats']:
            current_part.append("**🔓 ПУБЛИЧНЫЕ ГРУППЫ/КАНАЛЫ:**\n")
            
            for chat_data in results['public_chats'][:15]:
                badge = "🆕" if chat_data.get('source') in ['global_search', 'recommendations'] else "✓"
                chat_section = [
                    f"{badge} **{chat_data['name']}**",
                    f"   📝 Сообщений: {chat_data['count']}"
                ]
                
                for msg in chat_data['messages'][:max_messages_per_chat]:
                    date_str = msg['date'].strftime('%d.%m.%Y %H:%M')
                    text_preview = msg['text'][:70] + ('...' if len(msg['text']) > 70 else '')
                    chat_section.append(f"   • `{date_str}` {text_preview}")
                    if msg['link']:
                        chat_section.append(f"     [→ Перейти]({msg['link']})")
                
                if chat_data['count'] > max_messages_per_chat:
                    chat_section.append(f"   ... +{chat_data['count'] - max_messages_per_chat} ещё")
                
                chat_section.append("")
                
                # Проверка длины, если слишком длинно - начинаем новую часть
                test_length = len("\n".join(current_part + chat_section))
                if test_length > 3500:
                    parts.append("\n".join(current_part))
                    current_part = [f"**🔓 ПУБЛИЧНЫЕ ГРУППЫ (продолжение):**\n"]
                
                current_part.extend(chat_section)
        
        # Приватные чаты
        if results['private_chats']:
            if len("\n".join(current_part)) > 3000:
                parts.append("\n".join(current_part))
                current_part = []
            
            current_part.append("\n**🔒 ПРИВАТНЫЕ ГРУППЫ:**\n")
            
            for chat_data in results['private_chats'][:15]:
                chat_section = [
                    f"**{chat_data['name']}**",
                    f"📝 Сообщений: {chat_data['count']}"
                ]
                
                for msg in chat_data['messages'][:max_messages_per_chat]:
                    date_str = msg['date'].strftime('%d.%m.%Y %H:%M')
                    text_preview = msg['text'][:70] + ('...' if len(msg['text']) > 70 else '')
                    chat_section.append(f"• `{date_str}` {text_preview}")
                    if msg['link']:
                        chat_section.append(f"  [→]({msg['link']})")
                
                if chat_data['count'] > max_messages_per_chat:
                    chat_section.append(f"... +{chat_data['count'] - max_messages_per_chat} ещё")
                
                chat_section.append("")
                
                test_length = len("\n".join(current_part + chat_section))
                if test_length > 3500:
                    parts.append("\n".join(current_part))
                    current_part = [f"**🔒 ПРИВАТНЫЕ ГРУППЫ (продолжение):**\n"]
                
                current_part.extend(chat_section)
        
        # Добавляем последнюю часть
        if current_part:
            parts.append("\n".join(current_part))
        
        return parts
    
    async def process_search(self, event, query: str):
        """Обработка поискового запроса с максимальным охватом"""
        # Отправка уведомления о начале поиска
        status_msg = await event.respond(
            "🔍 **Запускаю глубокий поиск...**\n\n"
            "Это может занять 1-3 минуты.\n"
            "Ищу во всех доступных источниках... ⏳"
        )
        
        # Определение пользователя
        user = await self.resolve_user(query)
        
        if not user:
            await status_msg.edit(
                "❌ **Пользователь не найден**\n\n"
                "Проверьте правильность данных:\n"
                "• `@username` - для поиска по username\n"
                "• `+79991234567` - для поиска по телефону\n"
                "• `123456789` - для поиска по ID"
            )
            return
        
        # Обновление статуса
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        await status_msg.edit(
            f"✅ **Пользователь найден:** {user_name}\n\n"
            f"🔎 **Начинаю глубокий поиск...**\n"
            f"📡 Фаза 1: Ваши диалоги\n"
            f"🌐 Фаза 2: Глобальный поиск\n"
            f"🔍 Фаза 3: Публичные каналы\n\n"
            f"⏳ Ожидайте..."
        )
        
        try:
            # Глубокий поиск сообщений
            results = await self.deep_search_user_messages(user, limit_per_chat=100)
            
            # Форматирование результатов
            formatted_parts = await self.format_search_results(results, max_messages_per_chat=3)
            
            # Удаление статуса
            await status_msg.delete()
            
            # Отправка результатов частями
            for idx, part in enumerate(formatted_parts, 1):
                if len(formatted_parts) > 1:
                    header = f"📄 **Часть {idx}/{len(formatted_parts)}**\n\n"
                    await event.respond(header + part)
                else:
                    await event.respond(part)
                
                await asyncio.sleep(0.5)
            
            # Финальное сообщение
            await event.respond(
                f"✅ **Поиск завершён!**\n\n"
                f"Найдено **{results['total_messages']}** сообщений "
                f"в **{len(results['chats'])}** чатах."
            )
            
        except Exception as e:
            logger.error(f"Критическая ошибка при поиске: {e}")
            await status_msg.edit(
                f"❌ **Ошибка при поиске:**\n`{str(e)}`\n\n"
                f"Попробуйте позже или обратитесь к администратору."
            )


async def main():
    """Главная функция запуска бота"""
    bot = TelegramSearchBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == '__main__':
    asyncio.run(main())
