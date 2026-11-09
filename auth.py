#!/usr/bin/env python3
"""Новый вход в Telegram с сохранением сессии только в Railway."""

import argparse
import asyncio
from getpass import GetPassWarning, getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import warnings

from telethon import TelegramClient, functions
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession

LOG_PATH = os.path.join(tempfile.gettempdir(), "tsb-auth-log.txt")


def say(text=""):
    print(text, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(text + "\n")
    except OSError:
        pass


def ask(prompt, secret=False):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(f"> {prompt}\n")
    except OSError:
        pass
    value = getpass(prompt) if secret else input(prompt)
    return value.strip()


def railway(args, command, value=None):
    executable = shutil.which("npx.cmd") or shutil.which("npx")
    if not executable:
        raise RuntimeError("Не найден Node.js для запуска Railway.")
    result = subprocess.run(
        [executable, "--yes", "@railway/cli@latest", "variable", *command,
         "--project", args.project, "--service", args.service,
         "--environment", args.environment],
        input=value, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("Railway не выполнил операцию. Секреты не показаны.")
    return result.stdout


def old_sessions(response, api_id):
    return [
        entry for entry in response.authorizations
        if not entry.current and entry.api_id == api_id
    ]


async def revoke_old(client, api_id):
    old = old_sessions(await client(functions.account.GetAuthorizationsRequest()), api_id)
    if not old:
        say("Прежних подключений этого приложения нет.")
        return
    say("Отзываю прежние подключения этого приложения:")
    for entry in old:
        say(f"  {entry.device_model} | {entry.app_name} | создано {entry.date_created}")
    for entry in old:
        try:
            await client(functions.account.ResetAuthorizationRequest(hash=entry.hash))
        except RPCError as error:
            say(f"  автоматически не удалось ({type(error).__name__}).")
    alive = old_sessions(await client(functions.account.GetAuthorizationsRequest()), api_id)
    if alive:
        say("Прежний вход ещё активен. На телефоне откройте Telegram → Настройки → Устройства")
        say("и завершите:")
        for entry in alive:
            say(f"  {entry.device_model} | создано {entry.date_created}")
        say("Потом запустите вход снова — новый код не понадобится.")
        raise RuntimeError("Прежний вход не отозван.")
    say("Прежний вход отозван.")


async def main(args):
    say("Новый вход в Telegram. Код и пароль вводятся только в этом окне.")
    values = json.loads(await asyncio.to_thread(railway, args, ["list", "--json"]))
    say("Настройки Railway прочитаны.")
    api_id = int(values.get("API_ID") or 0)
    api_hash = values.get("API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("В Railway недоступны API_ID или API_HASH.")
    existing = values.get("USER_SESSION_STRING")
    del values

    client = TelegramClient(
        StringSession(existing or ""), api_id, api_hash,
        device_model="Telegram Search Bot - NEW Railway",
        system_version="Secure session", app_version="2.0",
        flood_sleep_threshold=0, request_retries=0,
    )
    saved = bool(existing)
    save_attempted = False
    try:
        say("Подключаюсь к Telegram…")
        await client.connect()
        if existing:
            me = await client.get_me()
            if me.bot or me.id != args.expected_user_id:
                raise RuntimeError("Сохранённый вход принадлежит другому аккаунту.")
            say(f"Сохранённый вход в порядке. ID аккаунта: {me.id}")
        else:
            phone = ask("Номер аккаунта-сканера с кодом страны: ")
            say("Запрашиваю код в Telegram…")
            sent = await client.send_code_request(phone)
            say("Код отправлен в Telegram.")
            code = ask("Код входа из Telegram (ввод скрыт): ", secret=True)
            say("Проверяю код…")
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                password = ask("Пароль двухэтапной защиты (ввод скрыт): ", secret=True)
                await client.sign_in(password=password)
                del password
            del phone, code, sent
            me = await client.get_me()
            if me.bot or me.id != args.expected_user_id:
                raise RuntimeError("Это другой аккаунт. Новый вход будет отменён.")
            say(f"Вход подтверждён. ID аккаунта: {me.id}")
            say("Сохраняю новый вход в Railway…")
            value = client.session.save()
            save_attempted = True
            await asyncio.to_thread(
                railway, args, ["set", "USER_SESSION_STRING", "--stdin", "--skip-deploys"], value,
            )
            check = json.loads(await asyncio.to_thread(railway, args, ["list", "--json"]))
            if check.get("USER_SESSION_STRING") != value:
                raise RuntimeError("Не удалось подтвердить сохранение входа в Railway.")
            del value, check
            saved = True
            say("Новый вход сохранён в Railway.")
        await revoke_old(client, api_id)
        say("Готово. Можно устанавливать обновление.")
    finally:
        if (not saved and not save_attempted and client.is_connected()
                and await client.is_user_authorized()):
            await client.log_out()
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdin = sys.__stdin__ = open("CONIN$", "r", encoding="utf-8")
        sys.stdout = sys.__stdout__ = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.__stderr__ = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    warnings.simplefilter("error", GetPassWarning)
    parser = argparse.ArgumentParser(description="Безопасный вход для бота поиска")
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--expected-user-id", type=int, required=True)
    args = parser.parse_args()
    exit_code = 1
    try:
        asyncio.run(main(args))
        exit_code = 0
    except (KeyboardInterrupt, EOFError):
        say("Вход отменён.")
    except Exception as error:
        say(f"Операция остановлена ({type(error).__name__}).")
        if isinstance(error, RuntimeError):
            say(str(error))
        elif isinstance(error, ConnectionError):
            say("Нет соединения с Telegram. Включите VPN и запустите вход снова.")
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as log:
                log.write("".join(traceback.format_exception(error)))
        except OSError:
            pass
        say("Если новый вход остался в списке устройств, не завершайте его до проверки сохранения.")
    ask("Нажмите Enter, чтобы закрыть окно: ")
    raise SystemExit(exit_code)
