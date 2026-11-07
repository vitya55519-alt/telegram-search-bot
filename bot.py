#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot для поиска комментариев пользователя в группах и чатах
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Union
from dotenv import load_dotenv

from telethon import TelegramClient, events, types, functions
from telethon.tl.types import User, Channel, Chat

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
        logger.info("Бот успешно запущен!")
        
        # Регистрация обработчиков
        self.client.add_event_handler(self.handle_start, events.NewMessage(pattern='/start'))
        self.client.add_event_handler(self.handle_help, events.NewMessage(pattern='/help'))
        self.client.add_event_handler(self.handle_search, events.NewMessage(pattern='/search'))
        self.client.add_event_handler(self.handle_find, events.NewMessage(pattern='/find'))
        
        # Ожидание событий
        await self.client.run_until_disconnected()
    
    def is_admin(self, user_id: int) -> bool:
        """Проверка, является ли пользователь администратором"""
        return user_id == self.admin_id
    
    async def handle_start(self, event):
        """Обработчик команды /start"""
        await event.respond(
            "👋 **Привет!**\n\n"
            "Я бот для поиска комментариев пользователей в Telegram.\n\n"
            "**Команды:**\n"
            "/search @username - поиск по username\n"
            "/search +79991234567 - поиск по телефону\n"
            "/search 123456789 - поиск по user ID\n"
            "/find <запрос> - то же самое\n"
            "/help - помощь\n\n"
            "⚠️ Бот может искать только в тех чатах, где он состоит или где вы можете искать."
        )
    
    async def handle_help(self, event):
        """Обработчик команды /help"""
        await event.respond(
            "📖 **Справка по использованию**\n\n"
            "**Форматы поиска:**\n"
            "• `/search @username` - по имени пользователя\n"
            "• `/search +79991234567` - по номеру телефона\n"
            "• `/search 123456789` - по User ID\n\n"
            "**Примеры:**\n"
            "`/search @durov`\n"
            "`/search +79991234567`\n"
            "`/search 12345678`\n\n"
            "**Ограничения:**\n"
            "• Бот ищет только в доступных вам чатах\n"
            "• Поиск может занять время для больших чатов\n"
            "• Некоторые приватные чаты могут быть недоступны\n\n"
            "💡 Для лучшей работы добавьте бота в нужные группы."
        )
    
    async def resolve_user(self, query: str) -> Optional[User]:
        """
        Определение пользователя по username, телефону или ID
        
        Args:
            query: username (@username), телефон (+79991234567) или ID (123456789)
            
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
                contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
                for user in contacts.users:
                    if hasattr(user, 'phone') and user.phone and f"+{user.phone}" == query:
                        return user
                return None
            
            # Поиск по ID
            else:
                user_id = int(query)
                user = await self.client.get_entity(user_id)
                return user if isinstance(user, User) else None
                
        except ValueError:
            logger.error(f"Неверный формат запроса: {query}")
            return None
        except Exception as e:
            logger.error(f"Ошибка при поиске пользователя {query}: {e}")
            return None
    
    async def search_user_messages(self, user: User, limit: int = 100) -> dict:
        """
        Поиск всех сообщений пользователя во всех доступных чатах (включая те, где не состоишь)
        
        Args:
            user: Объект пользователя
            limit: Максимальное количество сообщений для поиска в каждом чате
            
        Returns:
            Словарь с результатами поиска
        """
        results = {
            'user': user,
            'chats': [],
            'total_messages': 0,
            'private_chats': [],
            'public_chats': []
        }
        
        # Фаза 1: Поиск в доступных диалогах
        logger.info("📍 Фаза 1: Поиск в диалогах, где вы состоите...")
        dialogs = await self.client.get_dialogs()
        
        for dialog in dialogs:
            # Пропускаем личные диалоги
            if isinstance(dialog.entity, User):
                continue
            
            try:
                messages = []
                chat_name = dialog.name
                is_public = hasattr(dialog.entity, 'username') and dialog.entity.username
                
                # Поиск сообщений пользователя в чате
                async for message in self.client.iter_messages(
                    dialog.entity,
                    from_user=user.id,
                    limit=limit
                ):
                    link = None
                    if isinstance(dialog.entity, Channel):
                        if is_public:
                            link = f"https://t.me/{dialog.entity.username}/{message.id}"
                        else:
                            link = f"https://t.me/c/{str(dialog.entity.id)[4:]}/{message.id}"
                    
                    messages.append({
                        'id': message.id,
                        'date': message.date,
                        'text': message.text or '[Медиа/Стикер/Файл]',
                        'link': link,
                        'chat_id': dialog.entity.id
                    })
                
                if messages:
                    chat_info = {
                        'name': chat_name,
                        'id': dialog.entity.id,
                        'messages': messages,
                        'count': len(messages),
                        'public': is_public
                    }
                    results['chats'].append(chat_info)
                    if is_public:
                        results['public_chats'].append(chat_info)
                    else:
                        results['private_chats'].append(chat_info)
                    results['total_messages'] += len(messages)
                    
            except Exception as e:
                logger.debug(f"Ошибка при поиске в чате {dialog.name}: {e}")
                continue
        
        # Фаза 2: Поиск по публичному профилю пользователя
        logger.info("📍 Фаза 2: Поиск сообщений из публичного профиля...")
        try:
            if user.username:
                # Получить профиль пользователя
                profile = await self.client.get_entity(f"@{user.username}")
                
                # Попытка получить найденные сообщения пользователя через глобальный поиск
                search_results = await self.client(
                    functions.messages.SearchGlobalRequest(
                        q="",
                        offset_date=0,
                        offset_peer=types.InputPeerEmpty(),
                        offset_id=0,
                        limit=0
                    )
                )
        except Exception as e:
            logger.debug(f"Поиск по профилю не дал результатов: {e}")
        
        # Фаза 3: Попытка поиска через недавние контакты и рекомендации
        logger.info("📍 Фаза 3: Расширенный поиск в чатах...")
        try:
            # Получить последние контакты
            contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
            
            # Получить рекомендуемые чаты
            try:
                recommended = await self.client(functions.chatlists.GetChatlistSuggestionsRequest(exclude_mask=0))
                for chat in recommended.chats:
                    if isinstance(chat, Channel):
                        try:
                            chat_name = chat.title
                            is_public = hasattr(chat, 'username') and chat.username
                            
                            # Пропускаем уже проверенные чаты
                            if any(c['id'] == chat.id for c in results['chats']):
                                continue
                            
                            messages = []
                            async for message in self.client.iter_messages(
                                chat,
                                from_user=user.id,
                                limit=limit
                            ):
                                link = None
                                if is_public:
                                    link = f"https://t.me/{chat.username}/{message.id}"
                                
                                messages.append({
                                    'id': message.id,
                                    'date': message.date,
                                    'text': message.text or '[Медиа/Стикер/Файл]',
                                    'link': link,
                                    'chat_id': chat.id
                                })
                            
                            if messages:
                                chat_info = {
                                    'name': chat_name,
                                    'id': chat.id,
                                    'messages': messages,
                                    'count': len(messages),
                                    'public': is_public,
                                    'discovered': True
                                }
                                results['chats'].append(chat_info)
                                if is_public:
                                    results['public_chats'].append(chat_info)
                                results['total_messages'] += len(messages)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"Ошибка при получении рекомендуемых чатов: {e}")
        
        except Exception as e:
            logger.debug(f"Ошибка при расширенном поиске: {e}")
        
        return results
    
    async def format_search_results(self, results: dict, max_messages_per_chat: int = 5) -> str:
        """
        Форматирование результатов поиска для вывода
        
        Args:
            results: Результаты поиска
            max_messages_per_chat: Максимум сообщений на чат в выводе
            
        Returns:
            Отформатированная строка
        """
        user = results['user']
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        user_id = user.id
        
        output = [
            f"🔍 **Результаты поиска для {user_name}** (ID: `{user_id}`)\n",
            f"📊 Найдено сообщений: **{results['total_messages']}**",
            f"💬 В чатах: **{len(results['chats'])}**",
            f"🔓 Публичных групп: **{len(results['public_chats'])}**",
            f"🔒 Приватных групп: **{len(results['private_chats'])}**\n"
        ]
        
        if not results['chats']:
            output.append("❌ Сообщения не найдены.")
            return "\n".join(output)
        
        output.append("─" * 50 + "\n")
        
        # Разделить на публичные и приватные
        if results['public_chats']:
            output.append("**🔓 ПУБЛИЧНЫЕ ГРУППЫ/КАНАЛЫ:**\n")
            for chat_data in results['public_chats'][:10]:  # Ограничим до 10 для вывода
                badge = "✨" if chat_data.get('discovered') else "✓"
                output.append(f"{badge} **{chat_data['name']}**")
                output.append(f"   Сообщений: {chat_data['count']}\n")
                
                for i, msg in enumerate(chat_data['messages'][:max_messages_per_chat]):
                    date_str = msg['date'].strftime('%d.%m.%Y %H:%M')
                    text_preview = msg['text'][:80] + ('...' if len(msg['text']) > 80 else '')
                    
                    output.append(f"   • `{date_str}` - {text_preview}")
                    if msg['link']:
                        output.append(f"     [→]({msg['link']})")
                
                if chat_data['count'] > max_messages_per_chat:
                    output.append(f"   ... и еще {chat_data['count'] - max_messages_per_chat} сообщений")
                
                output.append("")
        
        if results['private_chats']:
            output.append("\n**🔒 ПРИВАТНЫЕ ГРУППЫ/ЧАТЫ (где вы состоите):**\n")
            for chat_data in results['private_chats'][:10]:
                output.append(f"**{chat_data['name']}**")
                output.append(f"Сообщений: {chat_data['count']}\n")
                
                for i, msg in enumerate(chat_data['messages'][:max_messages_per_chat]):
                    date_str = msg['date'].strftime('%d.%m.%Y %H:%M')
                    text_preview = msg['text'][:80] + ('...' if len(msg['text']) > 80 else '')
                    
                    output.append(f"• `{date_str}` - {text_preview}")
                    if msg['link']:
                        output.append(f"  [→]({msg['link']})")
                
                if chat_data['count'] > max_messages_per_chat:
                    output.append(f"  ... и еще {chat_data['count'] - max_messages_per_chat} сообщений")
                
                output.append("")
        
        total_shown = len(results['public_chats']) + len(results['private_chats'])
        if len(results['chats']) > total_shown:
            output.append(f"\n... и еще {len(results['chats']) - total_shown} чатов")
        
        return "\n".join(output)
    
    async def handle_search(self, event):
        """Обработчик команды /search"""
        await self.process_search(event)
    
    async def handle_find(self, event):
        """Обработчик команды /find"""
        await self.process_search(event)
    
    async def process_search(self, event):
        """Обработка поискового запроса"""
        # Извлечение запроса
        query = event.message.text.split(maxsplit=1)
        
        if len(query) < 2:
            await event.respond(
                "❌ Укажите username, телефон или ID пользователя.\n\n"
                "**Примеры:**\n"
                "`/search @username`\n"
                "`/search +79991234567`\n"
                "`/search 123456789`"
            )
            return
        
        search_query = query[1].strip()
        
        # Отправка уведомления о начале поиска
        status_msg = await event.respond("🔍 Ищу пользователя...")
        
        # Определение пользователя
        user = await self.resolve_user(search_query)
        
        if not user:
            await status_msg.edit("❌ Пользователь не найден. Проверьте правильность данных.")
            return
        
        # Обновление статуса
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        await status_msg.edit(f"✅ Пользователь найден: {user_name}\n🔎 Начинаю поиск сообщений...")
        
        # Поиск сообщений
        results = await self.search_user_messages(user, limit=100)
        
        # Форматирование и отправка результатов
        formatted_results = await self.format_search_results(results)
        
        await status_msg.delete()
        
        # Разбиение на части, если результат слишком длинный
        if len(formatted_results) > 4000:
            parts = [formatted_results[i:i+4000] for i in range(0, len(formatted_results), 4000)]
            for part in parts:
                await event.respond(part)
        else:
            await event.respond(formatted_results)


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
