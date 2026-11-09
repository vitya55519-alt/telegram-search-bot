import json
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)
MARKER = "#account"


def create_client(session_string, api_id, api_hash):
    return TelegramClient(
        StringSession(session_string), api_id, api_hash,
        flood_sleep_threshold=0, request_retries=0,
    )


async def load_accounts(client):
    accounts = []
    try:
        async for message in client.iter_messages("me", search=MARKER, limit=100):
            if not message.message or not message.message.startswith(MARKER):
                continue
            payload = message.message[len(MARKER):].strip()
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and data.get("session"):
                    accounts.append(data)
            except json.JSONDecodeError:
                logger.warning("Пропущена повреждённая запись аккаунта")
    except Exception as error:
        logger.error("Ошибка загрузки аккаунтов: %s", error)
    return accounts


async def save_account(client, data):
    text = f"{MARKER}\n{json.dumps(data, ensure_ascii=False)}"
    try:
        await client.send_message("me", text)
    except Exception as error:
        logger.error("Ошибка сохранения аккаунта: %s", error)
        raise


async def delete_account(client, account_id):
    try:
        async for message in client.iter_messages("me", search=MARKER, limit=100):
            if not message.message or not message.message.startswith(MARKER):
                continue
            payload = message.message[len(MARKER):].strip()
            try:
                data = json.loads(payload)
                if data.get("id") == account_id:
                    await client.delete_messages("me", message.id)
                    return True
            except json.JSONDecodeError:
                continue
    except Exception as error:
        logger.error("Ошибка удаления аккаунта: %s", error)
    return False
