#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram User Bot для глубокого поиска комментариев пользователя
Работает от одного авторизованного аккаунта
"""

import os
import asyncio
import logging
from typing import Optional, List
from dotenv import load_dotenv

from telethon import TelegramClient, events, types, functions
from telethon.tl.types import User, Channel, Chat
from telethon.errors import FloodWaitError, ChannelPrivateError, UserNotParticipantError

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')

if not all([API_ID, API_HASH]):
    raise ValueError("Заполните API_ID и API_HASH в .env файле!")


class TelegramSearchBot:
    def __init__(self):
        self.client = TelegramClient('user_session', API_ID, API_HASH)
        self.searching = False
        
    async def start(self):
        """Запуск бота"""
        await self.client.start()
        
        me = await self.client.get_me()
        logger.info(f"✅ Бот запущен: @{me.username or me.first_name} (ID: {me.id})")
        
        # Показываем статистику групп
        dialogs = await self.client.get_dialogs()
        groups_count = sum(1 for d in dialogs if not isinstance(d.entity, User))
        logger.info(f"📊 Доступно {groups_count} групп/каналов для поиска")
        
        self.client.add_event_handler(
            self.handle_search_message,
            events.NewMessage(incoming=True)
        )
        
        logger.info("✅ Готов к поиску! Напиши @username, +телефон или ID")
        await self.client.run_until_disconnected()
    
    async def resolve_user(self, query: str) -> Optional[User]:
        """Поиск пользователя"""
        try:
            if query.startswith('@'):
                user = await self.client.get_entity(query)
                return user if isinstance(user, User) else None
            
            elif query.startswith('+'):
                try:
                    contacts = await self.client(functions.contacts.GetContactsRequest(hash=0))
                    phone_clean = query.replace('+', '').replace(' ', '').replace('-', '')
                    for user in contacts.users:
                        if hasattr(user, 'phone') and user.phone:
                            user_phone = user.phone.replace(' ', '').replace('-', '')
                            if user_phone == phone_clean:
                                return user
                except:
                    pass
                return None
            
            elif query.isdigit():
                user_id = int(query)
                user = await self.client.get_entity(user_id)
                return user if isinstance(user, User) else None
            
            else:
                try:
                    result = await self.client(functions.contacts.SearchRequest(
                        q=query,
                        limit=10
                    ))
                    if result.users:
                        return result.users[0]
                except:
                    pass
                return None
                
        except Exception as e:
            logger.error(f"Ошибка поиска {query}: {e}")
            return None
    
    async def deep_search_user_messages(self, user: User) -> dict:
        """Глубокий поиск сообщений пользователя"""
        results = {
            'user': user,
            'chats': [],
            'total_messages': 0
        }
        
        all_chats = []
        processed_ids = set()
        
        logger.info(f"Сканирование всех диалогов...")
        try:
            async for dialog in self.client.iter_dialogs(limit=None):
                if isinstance(dialog.entity, User):
                    continue
                
                if dialog.entity.id not in processed_ids:
                    all_chats.append({
                        'entity': dialog.entity,
                        'name': dialog.name,
                        'id': dialog.entity.id,
                        'public': hasattr(dialog.entity, 'username') and dialog.entity.username,
                    })
                    processed_ids.add(dialog.entity.id)
            
            logger.info(f"Найдено {len(all_chats)} чатов для сканирования")
        except Exception as e:
            logger.error(f"Ошибка диалогов: {e}")
        
        logger.info(f"Начинаю поиск сообщений в {len(all_chats)} чатах...")
        
        for idx, chat_data in enumerate(all_chats, 1):
            try:
                if idx % 20 == 0:
                    logger.info(f"Прогресс: {idx}/{len(all_chats)}")
                
                entity = chat_data['entity']
                chat_name = chat_data['name']
                is_public = chat_data['public']
                
                messages = []
                
                try:
                    async for message in self.client.iter_messages(
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
                        })
                
                except (ChannelPrivateError, UserNotParticipantError):
                    continue
                except FloodWaitError as e:
                    logger.warning(f"FloodWait: {e.seconds}s, пауза...")
                    await asyncio.sleep(min(e.seconds, 10))
                    continue
                except Exception as e:
                    logger.debug(f"Ошибка в {chat_name}: {type(e).__name__}")
                    continue
                
                if messages:
                    results['chats'].append({
                        'name': chat_name,
                        'messages': messages,
                        'count': len(messages),
                    })
                    results['total_messages'] += len(messages)
                    logger.info(f"✅ {chat_name}: {len(messages)} сообщений")
                
                await asyncio.sleep(0.1)
                    
            except Exception as e:
                logger.debug(f"Ошибка обработки: {e}")
                continue
        
        logger.info(f"🎉 Найдено {results['total_messages']} сообщений в {len(results['chats'])} чатах")
        return results
    
    async def format_search_results(self, results: dict, max_messages: int = 5) -> List[str]:
        """Форматирование результатов"""
        user = results['user']
        user_name = f"@{user.username}" if user.username else user.first_name or "Unknown"
        user_id = user.id
        
        parts = []
        current = []
        
        header = [
            f"🔍 **{user_name}** (ID: `{user_id}`)",
            f"📊 **{results['total_messages']}** сообщений в **{len(results['chats'])}** чатах\n",
            "─" * 50 + "\n"
        ]
        
        current.extend(header)
        
        if not results['chats']:
            current.append("❌ Сообщения не найдены в доступных чатах.")
            parts.append("\n".join(current))
            return parts
        
        for chat in results['chats'][:25]:
            section = [
                f"**{chat['name']}**",
                f"📝 {chat['count']} сообщений"
            ]
            
            for msg in chat['messages'][:max_messages]:
                date = msg['date'].strftime('%d.%m %H:%M')
                text = msg['text'][:55] + ('...' if len(msg['text']) > 55 else '')
                section.append(f"• `{date}` {text}")
                if msg['link']:
                    section.append(f"  [→]({msg['link']})")
            
            if chat['count'] > max_messages:
                section.append(f"... +{chat['count'] - max_messages}")
            
            section.append("")
            current.extend(section)
            
            if len("\n".join(current)) > 3500:
                parts.append("\n".join(current))
                current = []
        
        if current:
            parts.append("\n".join(current))
        
        return parts
    
    async def handle_search_message(self, event):
        """Обработка входящих сообщений"""
        
        try:
            if event.out:
                return
            
            if not event.message.text:
                return
            
            query = event.message.text.strip()
            
            is_search = (
                query.startswith('@') or 
                query.startswith('+') or 
                (query.isdigit() and len(query) >= 5)
            )
            
            if not is_search:
                return
            
            if self.searching:
                await event.respond("⏳ Уже ищу, подожди...")
                return
            
            self.searching = True
            logger.info(f"🔍 Запрос: {query}")
            
            status = await event.respond(f"⏳ Ищу `{query}`...")
            
            try:
                user = await self.resolve_user(query)
                
                if not user:
                    await status.edit("❌ Пользователь не найден")
                    self.searching = False
                    return
                
                user_name = f"@{user.username}" if user.username else user.first_name
                await status.edit(f"🔎 Сканирую для {user_name}...")
                
                results = await self.deep_search_user_messages(user)
                formatted = await self.format_search_results(results, max_messages=4)
                
                try:
                    await status.delete()
                except:
                    pass
                
                for idx, part in enumerate(formatted, 1):
                    if len(formatted) > 1:
                        header = f"**📄 {idx}/{len(formatted)}**\n\n"
                        await event.respond(header + part)
                    else:
                        await event.respond(part)
                    
                    await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Ошибка поиска: {e}")
                try:
                    await status.edit(f"❌ Ошибка: {str(e)}")
                except:
                    pass
            
            finally:
                self.searching = False
        
        except Exception as e:
            logger.error(f"Ошибка обработчика: {e}")
            self.searching = False


async def main():
    bot = TelegramSearchBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        raise


if __name__ == '__main__':
    asyncio.run(main())
