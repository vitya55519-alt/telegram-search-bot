#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram User Bot для глубокого поиска комментариев пользователя
Поддержка нескольких аккаунтов для максимального охвата
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

# Получаем номера телефонов всех аккаунтов
ACCOUNT_PHONES = os.getenv('ACCOUNT_PHONES', '').split(',')
ACCOUNT_PHONES = [phone.strip() for phone in ACCOUNT_PHONES if phone.strip()]

# Проверка обязательных параметров
if not all([API_ID, API_HASH]):
    raise ValueError("Заполните API_ID и API_HASH в .env файле!")

if not ACCOUNT_PHONES:
    raise ValueError("Заполните ACCOUNT_PHONES в .env файле! (номера через запятую)")


class MultiAccountSearchBot:
    def __init__(self):
        self.clients = {}
        self.api_id = API_ID
        self.api_hash = API_HASH
        self.account_phones = ACCOUNT_PHONES
        
    async def init_clients(self):
        """Инициализация всех клиентов"""
        for idx, phone in enumerate(self.account_phones):
            try:
                session_name = f'user_session_{idx}'
                client = TelegramClient(session_name, self.api_id, self.api_hash)
                await client.start(phone=phone)
                
                me = await client.get_me()
                self.clients[phone] = client
                logger.info(f"✅ Аккаунт {idx+1}: @{me.username or me.first_name} ({phone}) подключен")
                
            except Exception as e:
                logger.error(f"❌ Ошибка подключения аккаунта {phone}: {e}")
    
    async def start(self):
        """Запуск бота со всеми аккаунтами"""
        await self.init_clients()
        
        if not self.clients:
            raise ValueError("Не удалось подключить ни один аккаунт!")
        
        logger.info(f"🚀 Запущено {len(self.clients)} аккаунтов")
        
        # Регистрируем обработчики для КАЖДОГО клиента
        for phone, client in self.clients.items():
            client.add_event_handler(
                lambda event, p=phone: self.handle_search_message(event, p),
                events.NewMessage(incoming=True)
            )
        
        # Ждем событий от всех клиентов
        await asyncio.gather(*[client.run_until_disconnected() for client in self.clients.values()])
    
    async def resolve_user(self, query: str, client: TelegramClient) -> Optional[User]:
        """Поиск пользователя"""
        try:
            if query.startswith('@'):
                user = await client.get_entity(query)
                return user if isinstance(user, User) else None
            
            elif query.startswith('+'):
                try:
                    contacts = await client(functions.contacts.GetContactsRequest(hash=0))
                    phone_clean = query.replace('+', '').replace(' ', '').replace('-', '')
                    for user in contacts.users:
                        if hasattr(user, 'phone') and user.phone:
                            user_phone = user.phone.replace(' ', '').replace('-', '')
                            if user_phone == phone_clean:
                                return user
                except Exception as e:
                    logger.debug(f"Поиск по телефону: {e}")
                
                return None
            
            elif query.isdigit():
                user_id = int(query)
                user = await client.get_entity(user_id)
                return user if isinstance(user, User) else None
            
            else:
                try:
                    result = await client(functions.contacts.SearchRequest(
                        q=query,
                        limit=10
                    ))
                    
                    if result.users:
                        return result.users[0]
                except Exception as e:
                    logger.debug(f"Поиск по имени: {e}")
                
                return None
                
        except Exception as e:
            logger.error(f"Ошибка поиска {query}: {e}")
            return None
    
    async def deep_search_user_messages(self, user: User, client: TelegramClient, account_num: int) -> dict:
        """Глубокий поиск от одного аккаунта"""
        results = {
            'user': user,
            'chats': [],
            'total_messages': 0,
            'account_num': account_num,
            'account_name': 'Аккаунт ' + str(account_num)
        }
        
        all_chats = []
        processed_ids = set()
        
        logger.info(f"   [{account_num}] Сканирование диалогов...")
        try:
            async for dialog in client.iter_dialogs(limit=None):
                if isinstance(dialog.entity, User):
                    continue
                
                if dialog.entity.id not in processed_ids:
                    all_chats.append({
                        'entity': dialog.entity,
                        'name': dialog.name,
                        'id': dialog.entity.id,
                        'public': hasattr(dialog.entity, 'username') and dialog.entity.username,
                        'source': 'dialogs'
                    })
                    processed_ids.add(dialog.entity.id)
            
            logger.info(f"   [{account_num}] Найдено {len(all_chats)} диалогов")
        except Exception as e:
            logger.error(f"   [{account_num}] Ошибка диалогов: {e}")
        
        # Поиск сообщений
        logger.info(f"   [{account_num}] Поиск в {len(all_chats)} чатах...")
        
        for idx, chat_data in enumerate(all_chats, 1):
            try:
                if idx % 20 == 0:
                    logger.info(f"   [{account_num}] Прогресс: {idx}/{len(all_chats)}")
                
                entity = chat_data['entity']
                chat_name = chat_data['name']
                is_public = chat_data['public']
                
                messages = []
                
                try:
                    async for message in client.iter_messages(
                        entity,
                        from_user=user.id,
                        limit=500
                    ):
                        link = None
                        if isinstance(entity, Channel):
                            if is_public and hasattr(entity, 'username') and entity.username:
                                link = f"https://t.me/{entity.username}/{message.id}"
                            else:
                                try:
                                    link = f"https://t.me/c/{str(entity.id)[4:]}/{message.id}"
                                except:
                                    pass
                        
                        messages.append({
                            'id': message.id,
                            'date': message.date,
                            'text': message.text or '[Медиа/Стикер/Файл]',
                            'link': link,
                            'chat_id': entity.id
                        })
                
                except (ChannelPrivateError, UserNotParticipantError):
                    continue
                except FloodWaitError as e:
                    logger.warning(f"   [{account_num}] FloodWait: {e.seconds}s")
                    await asyncio.sleep(min(e.seconds, 10))
                    continue
                except Exception as e:
                    logger.debug(f"   [{account_num}] Ошибка в {chat_name}: {type(e).__name__}")
                    continue
                
                if messages:
                    chat_info = {
                        'name': chat_name,
                        'id': entity.id,
                        'messages': messages,
                        'count': len(messages),
                        'public': is_public
                    }
                    
                    results['chats'].append(chat_info)
                    results['total_messages'] += len(messages)
                    logger.info(f"   [{account_num}] ✅ {chat_name}: {len(messages)} сообщений")
                
                await asyncio.sleep(0.1)
                    
            except Exception as e:
                logger.debug(f"Ошибка обработки чата: {e}")
                continue
        
        logger.info(f"   [{account_num}] Завершено: {results['total_messages']} сообщений")
        return results
    
    async def format_search_results(self, all_results: List[dict], max_messages: int = 5) -> List[str]:
        """Форматирование результатов от всех аккаунтов"""
        user = all_results[0]['user']
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        user_id = user.id
        
        total_messages = sum(r['total_messages'] for r in all_results)
        total_chats = sum(len(r['chats']) for r in all_results)
        
        parts = []
        current = []
        
        # Заголовок
        header = [
            f"🔍 **РЕЗУЛЬТАТЫ ПОИСКА: {user_name}**",
            f"👤 ID: `{user_id}`",
            f"📊 **Всего сообщений: {total_messages}**",
            f"💬 Чатов: {total_chats}",
            f"🔑 Аккаунтов использовано: {len(all_results)}\n",
            "─" * 50 + "\n"
        ]
        
        current.extend(header)
        
        if total_messages == 0:
            current.append("❌ Сообщения не найдены.")
            parts.append("\n".join(current))
            return parts
        
        # Результаты от каждого аккаунта
        for result in all_results:
            if result['total_messages'] == 0:
                continue
            
            current.append(f"**🔑 {result['account_name']} ({result['total_messages']} сообщений):**\n")
            
            for chat in result['chats'][:10]:
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
    
    async def handle_search_message(self, event, phone: str):
        """Обработка входящих сообщений"""
        
        # Игнорируем свои сообщения
        if event.out:
            return
        
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
        
        client = self.clients[phone]
        
        # Отправляем статус
        status = await event.respond(
            f"🔍 **Поиск:** `{query}`\n\n"
            f"⏳ Использую {len(self.clients)} аккаунта...\n"
            f"Это может занять 3-7 минут"
        )
        
        try:
            # Получаем пользователя
            user = await self.resolve_user(query, client)
            
            if not user:
                await status.edit("❌ Пользователь не найден")
                return
            
            user_name = f"@{user.username}" if user.username else user.first_name
            await status.edit(
                f"✅ Найден: {user_name}\n"
                f"🔎 Ищу от {len(self.clients)} аккаунтов...\n"
                f"⏳ Это займет время..."
            )
            
            # Глубокий поиск от ВСЕХ аккаунтов параллельно
            search_tasks = [
                self.deep_search_user_messages(user, acc_client, idx+1)
                for idx, (_, acc_client) in enumerate(self.clients.items())
            ]
            
            all_results = await asyncio.gather(*search_tasks)
            
            # Форматируем результаты
            formatted = await self.format_search_results(all_results, max_messages=5)
            
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
            total_msgs = sum(r['total_messages'] for r in all_results)
            total_chats = sum(len(r['chats']) for r in all_results)
            await event.respond(
                f"✅ **Поиск завершён!**\n"
                f"Найдено: **{total_msgs}** сообщений "
                f"в **{total_chats}** чатах "
                f"(от {len(self.clients)} аккаунтов)"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при поиске: {e}")
            try:
                await status.edit(f"❌ Ошибка: {str(e)}")
            except:
                pass


async def main():
    """Запуск"""
    bot = MultiAccountSearchBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Multi-Account Bot остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == '__main__':
    asyncio.run(main())
