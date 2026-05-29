import html
import logging
import os
import random
import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
HUI_API_BASE_URL = os.getenv("HUI_API_BASE_URL", "http://127.0.0.1:8081/hui").rstrip("/")
HUI_PUBLIC_BASE_URL = os.getenv("HUI_PUBLIC_BASE_URL", "https://keysforuuu.shop:8081").rstrip("/")
HUI_USERNAME = os.getenv("HUI_USERNAME", "")
HUI_PASSWORD = os.getenv("HUI_PASSWORD", "")
BOT_DB_PATH = os.getenv("BOT_DB_PATH", str(BASE_DIR / "bot.sqlite3"))

PASSWORD = "123123"
DEVICE_LIMIT = 3
QUOTA_BYTES = 1024**4

IMAGES = {
    "invite": BASE_DIR / "invitecode.png",
    "hello": BASE_DIR / "hello.png",
    "buy": BASE_DIR / "buysub.png",
    "paid": BASE_DIR / "oplati.png",
    "subs": BASE_DIR / "mysubs.png",
}


def quote(text: str) -> str:
    return f"<blockquote>{html.escape(text)}</blockquote>"


def quote_code(text: str) -> str:
    return f"<blockquote><code>{html.escape(text)}</code></blockquote>"


def is_admin(user_id: int) -> bool:
    return ADMIN_TELEGRAM_ID != 0 and user_id == ADMIN_TELEGRAM_ID


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(BOT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            create table if not exists users (
                telegram_id integer primary key,
                username text,
                invited integer not null default 0,
                created_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists invite_codes (
                code text primary key,
                uses_left integer not null,
                created_by integer not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists subscriptions (
                id integer primary key autoincrement,
                telegram_id integer not null,
                account_id integer not null,
                username text not null,
                sub_url text not null,
                months integer not null,
                quota integer not null,
                expire_time integer not null,
                created_at text not null
            )
            """
        )


def remember_user(user_id: int, username: str | None, invited: bool = False) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into users (telegram_id, username, invited, created_at)
            values (?, ?, ?, ?)
            on conflict(telegram_id) do update set
                username = excluded.username,
                invited = max(users.invited, excluded.invited)
            """,
            (user_id, username or "", 1 if invited else 0, datetime.now(timezone.utc).isoformat()),
        )


def user_has_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    with db() as conn:
        row = conn.execute("select invited from users where telegram_id = ?", (user_id,)).fetchone()
        return bool(row and row["invited"])


def activate_invite(code: str, user_id: int, username: str | None) -> bool:
    normalized = code.strip().upper()
    with db() as conn:
        row = conn.execute(
            "select uses_left from invite_codes where code = ?",
            (normalized,),
        ).fetchone()
        if not row or row["uses_left"] <= 0:
            return False
        conn.execute("update invite_codes set uses_left = uses_left - 1 where code = ?", (normalized,))
        conn.execute(
            """
            insert into users (telegram_id, username, invited, created_at)
            values (?, ?, 1, ?)
            on conflict(telegram_id) do update set username = excluded.username, invited = 1
            """,
            (user_id, username or "", datetime.now(timezone.utc).isoformat()),
        )
        return True


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Купить подписку", callback_data="buy")],
            [
                InlineKeyboardButton("Мои подписки", callback_data="subs"),
                InlineKeyboardButton("Профиль", callback_data="profile"),
            ],
        ]
    )


def buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 месяц", callback_data="term:1"),
                InlineKeyboardButton("2 месяца", callback_data="term:2"),
                InlineKeyboardButton("3 месяца", callback_data="term:3"),
            ],
            [InlineKeyboardButton("Назад в меню", callback_data="menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data="menu")]])


async def delete_message(update: Update) -> None:
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.delete()
        except BadRequest:
            pass


async def send_photo(update: Update, image: str, caption: str, keyboard: InlineKeyboardMarkup | None) -> None:
    target = update.effective_chat
    if not target:
        return
    with IMAGES[image].open("rb") as photo:
        await target.send_photo(
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def show_menu(update: Update) -> None:
    await send_photo(update, "hello", "", main_keyboard())


async def ask_invite(update: Update) -> None:
    remember_user(update.effective_user.id, update.effective_user.username)
    await send_photo(update, "invite", quote("Введите код приглашения:"), None)


class HuiApi:
    def __init__(self) -> None:
        self._token: str | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if self._token:
            headers["Authorization"] = self._token
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, f"{HUI_API_BASE_URL}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 20000:
            raise RuntimeError(payload.get("message") or "h-ui api error")
        return payload.get("data") or {}

    async def login(self) -> None:
        data = await self.request("POST", "/auth/login", json={"username": HUI_USERNAME, "pass": HUI_PASSWORD})
        self._token = f"{data['tokenType']} {data['accessToken']}"

    async def authed(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self._token:
            await self.login()
        try:
            return await self.request(method, path, **kwargs)
        except RuntimeError as exc:
            if "unauthorized" not in str(exc).lower() and "token" not in str(exc).lower():
                raise
            self._token = None
            await self.login()
            return await self.request(method, path, **kwargs)

    async def create_subscription(self, telegram_id: int, months: int) -> dict[str, Any]:
        username = random_username()
        con_pass = random_secret(12)
        expire = int((datetime.now(timezone.utc) + timedelta(days=30 * months)).timestamp() * 1000)
        remark = f"tg:{telegram_id}"
        await self.authed(
            "POST",
            "/account/saveAccount",
            json={
                "username": username,
                "pass": PASSWORD,
                "conPass": con_pass,
                "quota": QUOTA_BYTES,
                "expireTime": expire,
                "deviceNo": DEVICE_LIMIT,
                "deleted": 0,
                "remark": remark[:32],
            },
        )
        page = await self.authed(
            "GET",
            "/account/pageAccount",
            params={"pageNum": 1, "pageSize": 10, "username": username},
        )
        accounts = page.get("accountVos") or []
        account = next((item for item in accounts if item.get("username") == username), None)
        if not account:
            raise RuntimeError("created account not found")
        sub_url = f"{HUI_PUBLIC_BASE_URL}/sub/{account['subToken']}"
        return {
            "account_id": account["id"],
            "username": username,
            "sub_url": sub_url,
            "quota": QUOTA_BYTES,
            "expire_time": expire,
        }

    async def get_account(self, account_id: int) -> dict[str, Any]:
        return await self.authed("GET", "/account/getAccount", params={"id": account_id})


hui = HuiApi()


def random_username() -> str:
    return "vpn" + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))


def random_secret(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def store_subscription(telegram_id: int, item: dict[str, Any], months: int) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into subscriptions
                (telegram_id, account_id, username, sub_url, months, quota, expire_time, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                item["account_id"],
                item["username"],
                item["sub_url"],
                months,
                item["quota"],
                item["expire_time"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def format_bytes(value: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.2f} ТБ"


def format_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    remember_user(update.effective_user.id, update.effective_user.username, invited=is_admin(update.effective_user.id))
    if user_has_access(update.effective_user.id):
        await show_menu(update)
    else:
        await ask_invite(update)


async def new_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2 or not context.args[1].isdigit():
        await update.message.reply_text("Формат: /newcode HELLO 10")
        return
    code = context.args[0].strip().upper()
    uses = int(context.args[1])
    if uses <= 0:
        await update.message.reply_text("Количество использований должно быть больше 0.")
        return
    with db() as conn:
        conn.execute(
            """
            insert into invite_codes (code, uses_left, created_by, created_at)
            values (?, ?, ?, ?)
            on conflict(code) do update set uses_left = excluded.uses_left
            """,
            (code, uses, update.effective_user.id, datetime.now(timezone.utc).isoformat()),
        )
    await update.message.reply_text(f"Код {code} создан. Использований: {uses}")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if user_has_access(update.effective_user.id):
        await show_menu(update)
        return
    if activate_invite(update.message.text or "", update.effective_user.id, update.effective_user.username):
        await show_menu(update)
    else:
        await update.message.reply_text(quote("Неверный код приглашения."), parse_mode=ParseMode.HTML)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    if not user_has_access(update.effective_user.id):
        await delete_message(update)
        await ask_invite(update)
        return

    data = query.data or ""
    if data == "menu":
        await delete_message(update)
        await show_menu(update)
    elif data == "buy":
        await delete_message(update)
        caption = "\n".join(
            [
                "Выберите нужный срок подписки",
                "",
                "В каждой подписке можно:",
                quote("Использовать до 3-х устройств"),
                quote("Использовать 1 терабайт трафика"),
            ]
        )
        await send_photo(update, "buy", caption, buy_keyboard())
    elif data.startswith("term:"):
        months = int(data.split(":", 1)[1])
        await delete_message(update)
        caption = "Ваша ссылка для оплаты:\n" + quote("временно недоступно")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Я оплатил", callback_data=f"paid:{months}")]])
        await send_photo(update, "paid", caption, keyboard)
    elif data.startswith("paid:"):
        months = int(data.split(":", 1)[1])
        await query.edit_message_caption(caption="Создаю подписку...", reply_markup=None)
        try:
            item = await hui.create_subscription(update.effective_user.id, months)
            store_subscription(update.effective_user.id, item, months)
        except Exception:
            logging.exception("failed to create subscription")
            await query.edit_message_caption(
                caption="Не получилось создать подписку. Напишите администратору.",
                reply_markup=back_keyboard(),
            )
            return
        caption = "Ваша подписка:\n" + quote_code(item["sub_url"])
        await query.edit_message_caption(caption=caption, parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    elif data == "subs":
        await delete_message(update)
        await show_subscriptions(update)
    elif data.startswith("sub:"):
        await delete_message(update)
        await show_subscription_detail(update, int(data.split(":", 1)[1]))
    elif data == "profile":
        await delete_message(update)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Поддержка", callback_data="support")],
                [InlineKeyboardButton("Назад в меню", callback_data="menu")],
            ]
        )
        await send_photo(update, "hello", f"Ваш ID: <code>{update.effective_user.id}</code>", keyboard)
    elif data == "support":
        await query.answer("Поддержка пока недоступна.", show_alert=True)


async def show_subscriptions(update: Update) -> None:
    with db() as conn:
        rows = conn.execute(
            "select id, username, months from subscriptions where telegram_id = ? order by id desc",
            (update.effective_user.id,),
        ).fetchall()
    if not rows:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Назад в меню", callback_data="menu")]])
        await send_photo(update, "subs", "У вас пока нет подписок.", keyboard)
        return
    buttons = [[InlineKeyboardButton(f"{row['username']} - {row['months']} мес.", callback_data=f"sub:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton("Назад в меню", callback_data="menu")])
    await send_photo(update, "subs", "Ваши подписки:", InlineKeyboardMarkup(buttons))


async def show_subscription_detail(update: Update, sub_id: int) -> None:
    with db() as conn:
        row = conn.execute(
            "select * from subscriptions where id = ? and telegram_id = ?",
            (sub_id, update.effective_user.id),
        ).fetchone()
    if not row:
        await show_subscriptions(update)
        return
    try:
        account = await hui.get_account(row["account_id"])
        used = int(account.get("download", 0)) + int(account.get("upload", 0))
        quota = int(account.get("quota", row["quota"]))
        expire_time = int(account.get("expireTime", row["expire_time"]))
    except Exception:
        logging.exception("failed to refresh account")
        used = 0
        quota = int(row["quota"])
        expire_time = int(row["expire_time"])
    left = max(quota - used, 0)
    caption = "\n".join(
        [
            f"Подписка: {html.escape(row['username'])}",
            f"Осталось трафика: {format_bytes(left)}",
            f"Действует до: {format_date(expire_time)}",
        ]
    )
    await send_photo(update, "subs", caption, InlineKeyboardMarkup([[InlineKeyboardButton("Назад в меню", callback_data="menu")]]))


def validate_config() -> None:
    missing = [name for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_TELEGRAM_ID": ADMIN_TELEGRAM_ID,
        "HUI_USERNAME": HUI_USERNAME,
        "HUI_PASSWORD": HUI_PASSWORD,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Fill .env values: {', '.join(missing)}")
    for name, path in IMAGES.items():
        if not path.exists():
            raise RuntimeError(f"Image for {name} not found: {path}")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("start", "Запустить бота"),
            ("newcode", "Создать код приглашения"),
        ]
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    validate_config()
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["newcode", "newkey"], new_code))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
