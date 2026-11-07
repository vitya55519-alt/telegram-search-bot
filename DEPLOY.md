# 🚂 Деплой на Railway

## Подготовка

### 1. Получи Bot Token и User ID

**Bot Token от @BotFather:**
1. Напиши @BotFather в Telegram
2. Отправь `/newbot`
3. Следуй инструкциям (имя бота и username)
4. Скопируй токен (например: `7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`)

**Твой User ID от @userinfobot:**
1. Напиши @userinfobot в Telegram
2. Скопируй свой ID (например: `123456789`)

---

## Способ 1: Деплой через GitHub + Railway (Рекомендуется)

### Шаг 1: Создай GitHub репозиторий

```bash
cd telegram_search_bot
git init
git add .
git commit -m "Initial commit"
```

Создай репозиторий на GitHub и загрузи код:
```bash
git remote add origin https://github.com/твой_username/telegram-search-bot.git
git branch -M main
git push -u origin main
```

### Шаг 2: Деплой на Railway

1. Зайди на https://railway.app/
2. Войди через GitHub
3. Нажми **"New Project"**
4. Выбери **"Deploy from GitHub repo"**
5. Выбери свой репозиторий `telegram-search-bot`

### Шаг 3: Настрой переменные окружения

В Railway проекте:
1. Перейди на вкладку **"Variables"**
2. Добавь переменные:

```
API_ID=16346260
API_HASH=a8a6d0e647519e75c2ee91dbce07fa46
BOT_TOKEN=твой_токен_от_BotFather
ADMIN_ID=твой_user_id
```

3. Нажми **"Deploy"**

Готово! Бот запустится автоматически.

---

## Способ 2: Деплой напрямую через Railway CLI

### Шаг 1: Установи Railway CLI

**Windows:**
```bash
npm i -g @railway/cli
```

**macOS/Linux:**
```bash
curl -fsSL https://railway.app/install.sh | sh
```

### Шаг 2: Авторизация

```bash
railway login
```

### Шаг 3: Инициализация проекта

```bash
cd telegram_search_bot
railway init
```

### Шаг 4: Добавь переменные окружения

```bash
railway variables set API_ID=16346260
railway variables set API_HASH=a8a6d0e647519e75c2ee91dbce07fa46
railway variables set BOT_TOKEN=твой_токен
railway variables set ADMIN_ID=твой_user_id
```

### Шаг 5: Деплой

```bash
railway up
```

---

## Проверка работы

После деплоя:

1. Найди своего бота в Telegram по username
2. Отправь `/start`
3. Попробуй поиск: `/search @durov`

---

## Логи и мониторинг

**В Railway Dashboard:**
- Вкладка **"Deployments"** - история деплоев
- Вкладка **"Metrics"** - использование ресурсов
- Вкладка **"Logs"** - логи в реальном времени

**Через CLI:**
```bash
railway logs
```

---

## Обновление бота

**Через GitHub:**
```bash
git add .
git commit -m "Update bot"
git push
```
Railway автоматически задеплоит изменения.

**Через CLI:**
```bash
railway up
```

---

## Важные замечания

⚠️ **Файл сессии:**
- Railway создаст файл `bot_session.session` при первом запуске
- Этот файл будет храниться в контейнере
- При каждом новом деплое может потребоваться повторная авторизация

⚠️ **Лимиты Railway:**
- Free tier: $5 кредитов в месяц
- Для простого бота этого обычно хватает
- Бот будет работать 24/7

⚠️ **Безопасность:**
- НЕ коммить `.env` файл в Git (уже в `.gitignore`)
- Используй переменные окружения Railway
- API credentials уже добавлены в переменные

---

## Troubleshooting

**Бот не отвечает:**
```bash
railway logs
```
Проверь логи на ошибки

**Session истёк:**
Перезапусти сервис в Railway Dashboard

**Недостаточно памяти:**
В настройках Railway увеличь Memory Limit

---

## Полезные команды

```bash
# Посмотреть статус
railway status

# Посмотреть переменные
railway variables

# Открыть Dashboard
railway open

# Остановить проект
railway down
```

---

🎉 **Готово!** Твой бот работает в облаке 24/7!
