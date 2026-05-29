# VPN Telegram bot

Бот продает подписки через h-ui API, хранит invite-коды и связи Telegram-пользователей с подписками в `bot.sqlite3`.

## Установка на ВДС

```bash
cd /path/to/tgbot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

В `.env` обязательно заполнить `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `HUI_USERNAME`, `HUI_PASSWORD`.

Если панель открыта на `https://keysforuuu.shop:8081`, но бот стоит рядом на сервере, обычно достаточно:

```env
HUI_API_BASE_URL=http://127.0.0.1:8081/hui
HUI_PUBLIC_BASE_URL=https://keysforuuu.shop:8081
```

## Админ-команды

```text
/newcode HELLO 10
/newkey HELLO 10
```

Обе команды создают invite-код `HELLO` на 10 использований. Админ из `ADMIN_TELEGRAM_ID` проходит без кода.

## systemd

Пример сервиса лежит в `vpn-tgbot.service.example`:

```ini
[Unit]
Description=VPN Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/tgbot
ExecStart=/path/to/tgbot/.venv/bin/python /path/to/tgbot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
