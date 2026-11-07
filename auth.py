#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram User Bot - авторизация для нового аккаунта
"""

import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH', '')

if not all([API_ID, API_HASH]):
    raise ValueError("Заполните API_ID и API_HASH в .env файле!")

async def main():
    # Создаём новую сессию с именем "main_account"
    client = TelegramClient('main_account', API_ID, API_HASH)
    await client.start()
    
    me = await client.get_me()
    print(f"\n✅ Авторизован: @{me.username or me.first_name}")
    print(f"ID: {me.id}\n")
    
    await client.disconnect()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
