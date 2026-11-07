#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram User Bot для глубокого поиска комментариев пользователя
Работает от личного аккаунта - максимальный доступ к чатам и истории
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, List
from dotenv import load_dotenv

from telethon import TelegramClient, events, types, functions
from telethon.tl.types import User, Channel, Chat
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

# Проверка обязательных параметров
if not all([API_ID, API_HASH]):
    raise ValueError("Заполните API_ID и API_HASH в .env файле!")


class TelegramUserSearchBot:
    def __init__(self):
        self.client = TelegramClient('user_session', API_ID, API_HASH)
        self.search_active = False
        
    async def start(self):
        """Запуск бота"""
        await self.client.start()
        logger.info("🚀 User Bot успешно запущен!")
        logger.info("✅ Подключен от личного аккаунта - полный доступ к чатам")
        
        # Регистрация обработчиков
        self.client.add_event_handler(self.handle_search_message, events.NewMessage(incoming=True))
        
        # Ожидание событий
        await self.client.run_until_disconnected()
    
    async def resolve_user(self, query: str) -> Optional[User]:
        """
        Определение пользователя по username, телефону, ID или имени
        """
        try:
            # Поиск по username
            if query.startswith('@'):
                user = await self.client.get_entity(query)
                return user if isinstance(user, User) else None
            
            # Поиск по телефону
            elif query.startswith('+'):
                try:
                    contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
                    phone_clean = query.replace('+', '').replace(' ', '').replace('-', '')
                    for user in contacts.users:
                        if hasattr(user, 'phone') and user.phone:
                            user_phone = user.phone.replace(' ', '').replace('-', '')
                            if user_phone == phone_clean:
                                return user
                except Exception as e:
                    logger.debug(f"Поиск по телефону не удался: {e}")
                
                return None
            
            # Поиск по ID
            elif query.isdigit():
                user_id = int(query)
                user = await self.client.get_entity(user_id)
                return user if isinstance(user, User) else None
            
            # Поиск по имени
            else:
                try:
                    result = await self.client(functions.contacts.SearchRequest(
                        q=query,
                        limit=10
                    ))
                    
                    if result.users:
                        return result.users[0]
                except Exception as e:
                    logger.debug(f"Поиск по имени не дал результатов: {e}")
                
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при поиске пользователя {query}: {e}")
            return None
    
    async def deep_search_user_messages(self, user: User) -> dict:
        """
        МАКСИМАЛЬНЫЙ глубокий поиск всех сообщений пользователя везде
        """
        results = {
            'user': user,
            'chats': [],
            'total_messages': 0,
            'private_chats': [],
            'public_chats': [],
            'channels': [],
            'search_errors': []
        }
        
        all_chats = []
        processed_ids = set()
        
        # === ФАЗА 1: Все доступные диалоги ===
        logger.info("📍 Фаза 1: Сканирование всех диалогов...")
        try:
            # Получаем ВСЕ диалоги без лимита
            async for dialog in self.client.iter_dialogs(limit=None):
                if isinstance(dialog.entity, User):
                    continue
                
                if dialog.entity.id not in processed_ids:
                    is_public = hasattr(dialog.entity, 'username') and dialog.entity.username
                    entity_type = 'channel' if isinstance(dialog.entity, Channel) else 'group'
                    
                    all_chats.append({
                        'entity': dialog.entity,
                        'name': dialog.name,
                        'id': dialog.entity.id,
                        'public': is_public,
                        'type': entity_type,
                        'source': 'dialogs'
                    })
                    processed_ids.add(dialog.entity.id)
            
            logger.info(f"✅ Фаза 1 завершена: {len(all_chats)} диалогов найдено")
        except Exception as e:
            logger.error(f"Ошибка при получении диалогов: {e}")
            results['search_errors'].append(f"Ошибка диалогов: {str(e)}")
        
        # === ФАЗА 2: Поиск через контакты и недавние ===
        logger.info("📍 Фаза 2: Поиск через контакты и историю...")
        try:
            # Получаем контакты и пытаемся найти их чаты
            contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
            for contact in contacts.users[:100]:
                try:
                    # Получаем чаты для каждого контакта через их профиль
                    user_full = await self.client(functions.users.GetFullUserRequest(contact.id))
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            
            logger.info("✅ Фаза 2 завершена")
        except Exception as e:
            logger.debug(f"Ошибка при поиске через контакты: {e}")
        
        # === ФАЗА 3: Глобальный поиск публичных чатов ===
        logger.info("📍 Фаза 3: Глобальный поиск публичных чатов...")
        try:
            # Пытаемся получить рекомендованные и популярные чаты
            search_topics = ['news', 'tech', 'мемы', 'новости', 'chat', 'group']
            
            for topic in search_topics:
                try:
                    result = await self.client(functions.contacts.SearchRequest(
                        q=topic,
                        limit=50
                    ))
                    
                    for chat in result.chats:
                        if isinstance(chat, Channel) and chat.id not in processed_ids:
                            all_chats.append({
                                'entity': chat,
                                'name': chat.title,
                                'id': chat.id,
                                'public': hasattr(chat, 'username') and chat.username,
                                'type': 'channel',
                                'source': 'global_search'
                            })
                            processed_ids.add(chat.id)
                    
                    await asyncio.sleep(1)  # Rate limiting
                except FloodWaitError as e:
                    logger.warning(f"FloodWait: ждем {e.seconds} сек")
                    await asyncio.sleep(min(e.seconds, 5))
                except Exception as e:
                    logger.debug(f"Ошибка при поиске {topic}: {e}")
            
            logger.info(f"✅ Фаза 3 завершена: {len(all_chats)} всего чатов")
        except Exception as e:
            logger.debug(f"Ошибка при глобальном поиске: {e}")
        
        # === ОСНОВНОЙ ПОИСК: Сканирование всех найденных чатов ===
        logger.info(f"🔍 ПОИСК сообщений в {len(all_chats)} чатах...")
        logger.info(f"   Ищу пользователя ID: {user.id}")
        
        for idx, chat_data in enumerate(all_chats, 1):
            try:
                if idx % 20 == 0:
                    logger.info(f"   Прогресс: {idx}/{len(all_chats)}")
                
                entity = chat_data['entity']
                chat_name = chat_data['name']
                is_public = chat_data['public']
                chat_type = chat_data['type']
                
                messages = []
                
                # Ищем сообщения от пользователя
                try:
                    async for message in self.client.iter_messages(
                        entity,
                        from_user=user.id,
                        limit=500  # Увеличиваем лимит
                    ):
                        link = None
                        if isinstance(entity, Channel):
                            if is_public and hasattr(entity, 'username') and entity.username:
                                link = f"https://t.me/{entity.username}/{message.id}"
                            else:
                                # Для приватных каналов
                                try:
                                    link = f"https://t.me/c/{str(entity.id)[4:]}/{message.id}"
                                except:
                                    pass
                        
                        messages.append({
                            'id': message.id,
                            'date': message.date,
                            'text': message.text or '[Медиа/Стикер/Файл/Голосовое]',
                            'link': link,
                            'chat_id': entity.id
                        })
                
                except (ChannelPrivateError, UserNotParticipantError):
                    logger.debug(f"   ❌ Нет доступа: {chat_name}")
                    continue
                except FloodWaitError as e:
                    logger.warning(f"   ⏱ FloodWait в {chat_name}: {e.seconds}s")
                    await asyncio.sleep(min(e.seconds, 10))
                    continue
                except Exception as e:
                    logger.debug(f"   ⚠️  Ошибка в {chat_name}: {type(e).__name__}")
                    continue
                
                # Если нашли сообщения - добавляем в результаты
                if messages:
                    chat_info = {
                        'name': chat_name,
                        'id': entity.id,
                        'messages': messages,
                        'count': len(messages),
                        'public': is_public,
                        'type': chat_type,
                        'source': chat_data['source']
                    }
                    
                    results['chats'].append(chat_info)
                    
                    if chat_type == 'channel':
                        results['channels'].append(chat_info)
                    elif is_public:
                        results['public_chats'].append(chat_info)
                    else:
                        results['private_chats'].append(chat_info)
                    
                    results['total_messages'] += len(messages)
                    logger.info(f"   ✅ {chat_name}: {len(messages)} сообщений")
                
                # Задержка для избежания rate limits
                await asyncio.sleep(0.2)
                    
            except Exception as e:
                logger.debug(f"Ошибка при обработке чата: {e}")
                continue
        
        logger.info(f"🎉 ПОИСК ЗАВЕРШЕН: {results['total_messages']} сообщений в {len(results['chats'])} чатах")
        return results
    
    async def format_search_results(self, results: dict, max_messages: int = 5) -> List[str]:
        """Форматирование результатов"""
        user = results['user']
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        user_id = user.id
        
        parts = []
        current = []
        
        # Заголовок
        header = [
            f"🔍 **РЕЗУЛЬТАТЫ ПОИСКА: {user_name}**",
            f"👤 ID: `{user_id}`",
            f"📊 **Всего сообщений: {results['total_messages']}**",
            f"💬 Чатов: {len(results['chats'])}",
            f"   • Каналы: {len(results['channels'])}",
            f"   • Публичные группы: {len(results['public_chats'])}",
            f"   • Приватные группы: {len(results['private_chats'])}\n",
            "─" * 50 + "\n"
        ]
        
        current.extend(header)
        
        if not results['chats']:
            current.append("❌ Сообщения не найдены.")
            parts.append("\n".join(current))
            return parts
        
        # Каналы
        if results['channels']:
            current.append("**📡 КАНАЛЫ:**\n")
            for chat in results['channels'][:10]:
                section = [
                    f"✓ **{chat['name']}**",
                    f"  Сообщений: {chat['count']}"
                ]
                
                for msg in chat['messages'][:max_messages]:
                    date = msg['date'].strftime('%d.%m %H:%M')
                    text = msg['text'][:60] + ('...' if len(msg['text']) > 60 else '')
                    section.append(f"  • `{date}` {text}")
                    if msg['link']:
                        section.append(f"    [→]({msg['link']})")
                
                if chat['count'] > max_messages:
                    section.append(f"  ... +{chat['count'] - max_messages}")
                
                section.append("")
                current.extend(section)
                
                if len("\n".join(current)) > 3500:
                    parts.append("\n".join(current))
                    current = []
        
        # Публичные группы
        if results['public_chats']:
            current.append("\n**🔓 ПУБЛИЧНЫЕ ГРУППЫ:**\n")
            for chat in results['public_chats'][:10]:
                section = [
                    f"✓ **{chat['name']}**",
                    f"  Сообщений: {chat['count']}"
                ]
                
                for msg in chat['messages'][:max_messages]:
                    date = msg['date'].strftime('%d.%m %H:%M')
                    text = msg['text'][:60] + ('...' if len(msg['text']) > 60 else '')
                    section.append(f"  • `{date}` {text}")
                    if msg['link']:
                        section.append(f"    [→]({msg['link']})")
                
                if chat['count'] > max_messages:
                    section.append(f"  ... +{chat['count'] - max_messages}")
                
                section.append("")
                current.extend(section)
                
                if len("\n".join(current)) > 3500:
                    parts.append("\n".join(current))
                    current = []
        
        # Приватные группы
        if results['private_chats']:
            current.append("\n**🔒 ПРИВАТНЫЕ ГРУППЫ:**\n")
            for chat in results['private_chats'][:10]:
                section = [
                    f"✓ **{chat['name']}**",
                    f"  Сообщений: {chat['count']}"
                ]
                
                for msg in chat['messages'][:max_messages]:
                    date = msg['date'].strftime('%d.%m %H:%M')
                    text = msg['text'][:60] + ('...' if len(msg['text']) > 60 else '')
                    section.append(f"  • `{date}` {text}")
                    if msg['link']:
                        section.append(f"    [→]({msg['link']})")
                
                if chat['count'] > max_messages:
                    section.append(f"  ... +{chat['count'] - max_messages}")
                
                section.append("")
                current.extend(section)
                
                if len("\n".join(current)) > 3500:
                    parts.append("\n".join(current))
                    current = []
        
        if current:
            parts.append("\n".join(current))
        
        return parts
    
    async def handle_search_message(self, event):
        """Обработка входящих сообщений - автоматический поиск"""
        
        # Игнорируем свои сообщения
        if event.out:
            return
        
        # Игнорируем пересылки и не-текстовые
        if not event.message.text:
            return
        
        query = event.message.text.strip()
        
        # Проверяем формат поиска
        is_search = (
            query.startswith('@') or 
            query.startswith('+') or 
            (query.isdigit() and len(query) >= 5)
        )
        
        if not is_search:
            return
        
        logger.info(f"🔍 Получена команда поиска: {query}")
        
        # Отправляем статус
        status = await event.respond(
            f"🔍 **Поиск:** `{query}`\n\n"
            f"⏳ Сканирую все диалоги...\n"
            f"Это может занять 2-5 минут"
        )
        
        try:
            # Получаем пользователя
            user = await self.resolve_user(query)
            
            if not user:
                await status.edit("❌ Пользователь не найден")
                return
            
            user_name = f"@{user.username}" if user.username else user.first_name
            await status.edit(
                f"✅ Найден: {user_name}\n"
                f"🔎 Ищу все сообщения...\n"
                f"⏳ Это займет время..."
            )
            
            # Глубокий поиск
            results = await self.deep_search_user_messages(user)
            
            # Форматируем результаты
            formatted = await self.format_search_results(results, max_messages=5)
            
            # Удаляем статус
            try:
                await status.delete()
            except:
                pass
            
            # Отправляем результаты
            for idx, part in enumerate(formatted, 1):
                if len(formatted) > 1:
                    header = f"**📄 Часть {idx}/{len(formatted)}**\n\n"
                    await event.respond(header + part)
                else:
                    await event.respond(part)
                
                await asyncio.sleep(1)
            
            # Финальное сообщение
            await event.respond(
                f"✅ **Поиск завершён!**\n"
                f"Найдено: **{results['total_messages']}** сообщений "
                f"в **{len(results['chats'])}** чатах"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при поиске: {e}")
            try:
                await status.edit(f"❌ Ошибка: {str(e)}")
            except:
                pass


async def main():
    """Запуск"""
    bot = TelegramUserSearchBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("User Bot остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == '__main__':
    asyncio.run(main())
