import asyncio
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
from typing import Any, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from dotenv import load_dotenv


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
MAX_SUBSCRIPTIONS_PER_USER = 3
QUOTA_BYTES = 1024**4
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-06-01-28"
USER_AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-06-01-22"

IMAGES = {
    "invite": BASE_DIR / "invitecode.png",
    "hello": BASE_DIR / "hello.png",
    "buy": BASE_DIR / "buysub.png",
    "payment": BASE_DIR / "oplati.png",
    "paid": BASE_DIR / "paid.png",
    "subs": BASE_DIR / "mysubs.png",
    "agreement": BASE_DIR / "soglas.png",
}

router = Router()


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
                accepted_terms integer not null default 0,
                created_at text not null
            )
            """
        )
        columns = {row["name"] for row in conn.execute("pragma table_info(users)").fetchall()}
        if "accepted_terms" not in columns:
            conn.execute("alter table users add column accepted_terms integer not null default 0")
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


def remember_user(
    user_id: int,
    username: Optional[str],
    invited: bool = False,
    accepted_terms: Optional[bool] = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into users (telegram_id, username, invited, accepted_terms, created_at)
            values (?, ?, ?, ?, ?)
            on conflict(telegram_id) do update set
                username = excluded.username,
                invited = max(users.invited, excluded.invited),
                accepted_terms = case
                    when excluded.accepted_terms = 1 then 1
                    else users.accepted_terms
                end
            """,
            (
                user_id,
                username or "",
                1 if invited else 0,
                1 if accepted_terms else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def user_has_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    with db() as conn:
        row = conn.execute("select invited from users where telegram_id = ?", (user_id,)).fetchone()
        return bool(row and row["invited"])


def user_accepted_terms(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    with db() as conn:
        row = conn.execute("select accepted_terms from users where telegram_id = ?", (user_id,)).fetchone()
        return bool(row and row["accepted_terms"])


def activate_invite(code: str, user_id: int, username: Optional[str]) -> bool:
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
            insert into users (telegram_id, username, invited, accepted_terms, created_at)
            values (?, ?, 1, 1, ?)
            on conflict(telegram_id) do update set
                username = excluded.username,
                invited = 1,
                accepted_terms = 1
            """,
            (user_id, username or "", datetime.now(timezone.utc).isoformat()),
        )
        return True


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def legal_buttons() -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="Политика конфиденциальности", url=PRIVACY_POLICY_URL),
            InlineKeyboardButton(text="Пользовательское соглашение", url=USER_AGREEMENT_URL),
        ]
    ]


def main_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🛒 Купить подписку", "buy")],
            [
                button("📦 Мои подписки", "subs"),
                button("👤 Профиль", "profile"),
            ],
            *legal_buttons(),
        ]
    )


def agreement_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            *legal_buttons(),
            [button("✅ Принимаю", "accept_terms")],
        ]
    )


def buy_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button("1️⃣ 1 месяц", "term:1"),
                button("2️⃣ 2 месяца", "term:2"),
                button("3️⃣ 3 месяца", "term:3"),
            ],
            [button("⬅️ Назад в меню", "menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return keyboard([[button("🏠 Вернуться в меню", "menu")]])


def subscription_action_keyboard(sub_url: str, back_callback: str = "menu") -> InlineKeyboardMarkup:
    return keyboard(
        [
            [InlineKeyboardButton(text="Инструкция по подключению", url=sub_url)],
            [button("⬅️ Назад в меню", back_callback)],
        ]
    )


def get_callback_message(query: CallbackQuery) -> Optional[Message]:
    return query.message if isinstance(query.message, Message) else None


async def delete_message(message: Optional[Message]) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def send_photo(
    message: Message,
    image: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    await message.answer_photo(
        photo=FSInputFile(IMAGES[image]),
        caption=caption or None,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def edit_caption(
    message: Message,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    try:
        await message.edit_caption(caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


async def show_menu(message: Message) -> None:
    await send_photo(message, "hello", "", main_keyboard())


async def show_agreement(message: Message) -> None:
    await send_photo(message, "agreement", "", agreement_keyboard())


async def ask_invite(message: Message, user: User) -> None:
    remember_user(user.id, user.username, accepted_terms=True)
    await send_photo(message, "invite", quote("Введите код приглашения:"), None)


class HuiApi:
    def __init__(self) -> None:
        self._token: Optional[str] = None

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
        accounts = page.get("records") or page.get("accountVos") or []
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


def subscription_count(telegram_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "select count(*) as total from subscriptions where telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return int(row["total"]) if row else 0


def format_bytes(value: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.2f} ТБ"


def format_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


@router.message(Command("start"))
async def start(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    remember_user(user.id, user.username, invited=is_admin(user.id), accepted_terms=is_admin(user.id))
    if user_accepted_terms(user.id):
        if user_has_access(user.id):
            await show_menu(message)
        else:
            await ask_invite(message, user)
        return
    await show_agreement(message)


@router.message(Command("newcode", "newkey"))
async def new_code(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Формат: /newcode HELLO 10")
        return
    code = args[0].strip().upper()
    uses = int(args[1])
    if uses <= 0:
        await message.answer("Количество использований должно быть больше 0.")
        return
    with db() as conn:
        conn.execute(
            """
            insert into invite_codes (code, uses_left, created_by, created_at)
            values (?, ?, ?, ?)
            on conflict(code) do update set uses_left = excluded.uses_left
            """,
            (code, uses, user.id, datetime.now(timezone.utc).isoformat()),
        )
    await message.answer(f"Код {html.escape(code)} создан. Использований: {uses}")


@router.message(F.text & ~F.text.startswith("/"))
async def text_message(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not user_accepted_terms(user.id):
        await show_agreement(message)
        return
    if user_has_access(user.id):
        await show_menu(message)
        return
    if activate_invite(message.text or "", user.id, user.username):
        await show_menu(message)
    else:
        await message.answer(quote("Неверный код приглашения."), parse_mode=ParseMode.HTML)


@router.callback_query()
async def callback(query: CallbackQuery) -> None:
    message = get_callback_message(query)
    await query.answer()
    if not message:
        return

    user = query.from_user
    data = query.data or ""

    if data == "accept_terms":
        remember_user(user.id, user.username, invited=is_admin(user.id), accepted_terms=True)
        await delete_message(message)
        if user_has_access(user.id):
            await show_menu(message)
        else:
            await ask_invite(message, user)
        return

    if not user_accepted_terms(user.id):
        await delete_message(message)
        await show_agreement(message)
        return

    if not user_has_access(user.id):
        await delete_message(message)
        await ask_invite(message, user)
        return

    if data == "menu":
        await delete_message(message)
        await show_menu(message)
    elif data == "buy":
        if subscription_count(user.id) >= MAX_SUBSCRIPTIONS_PER_USER:
            await query.answer("На один Telegram-аккаунт можно оформить максимум 3 подписки.", show_alert=True)
            return
        await delete_message(message)
        caption = "\n".join(
            [
                "Выберите нужный срок подписки",
                "",
                "В каждой подписке можно:",
                quote("Использовать до 3-х устройств"),
                quote("Современный протокол Hysteria 2"),
                quote("Использовать 1 терабайт трафика"),
            ]
        )
        await send_photo(message, "buy", caption, buy_keyboard())
    elif data.startswith("term:"):
        try:
            months = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный срок подписки.", show_alert=True)
            return
        await delete_message(message)
        caption = "Ваша ссылка для оплаты:\n" + quote("временно недоступно")
        reply_markup = keyboard([[button("✅ Я оплатил", f"paid:{months}")]])
        await send_photo(message, "payment", caption, reply_markup)
    elif data.startswith("paid:"):
        try:
            months = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный срок подписки.", show_alert=True)
            return
        if subscription_count(user.id) >= MAX_SUBSCRIPTIONS_PER_USER:
            await edit_caption(message, "На один Telegram-аккаунт можно оформить максимум 3 подписки.", back_keyboard())
            return
        await edit_caption(message, "Создаю подписку...", None)
        try:
            item = await hui.create_subscription(user.id, months)
            store_subscription(user.id, item, months)
        except Exception:
            logging.exception("failed to create subscription")
            await edit_caption(message, "Не получилось создать подписку. Напишите администратору.", back_keyboard())
            return
        caption = "Оплата подтверждена. Ваша подписка:\n" + quote_code(item["sub_url"])
        await delete_message(message)
        await send_photo(message, "paid", caption, subscription_action_keyboard(item["sub_url"]))
    elif data == "subs":
        await delete_message(message)
        await show_subscriptions(message, user.id)
    elif data.startswith("sub:"):
        try:
            sub_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Подписка не найдена.", show_alert=True)
            return
        await delete_message(message)
        await show_subscription_detail(message, user.id, sub_id)
    elif data == "profile":
        await delete_message(message)
        reply_markup = keyboard(
            [
                [button("🛟 Поддержка", "support")],
                [button("⬅️ Назад в меню", "menu")],
            ]
        )
        await send_photo(message, "hello", f"Ваш ID: <code>{user.id}</code>", reply_markup)
    elif data == "support":
        await query.answer("Поддержка пока недоступна.", show_alert=True)


async def show_subscriptions(message: Message, user_id: int) -> None:
    with db() as conn:
        rows = conn.execute(
            "select id, username, months from subscriptions where telegram_id = ? order by id desc",
            (user_id,),
        ).fetchall()
    if not rows:
        await send_photo(message, "subs", "У вас пока нет подписок.", back_keyboard())
        return
    buttons = [
        [button(f"🔑 {row['username']} · {row['months']} мес.", f"sub:{row['id']}")]
        for row in rows
    ]
    buttons.append([button("⬅️ Назад в меню", "menu")])
    await send_photo(message, "subs", "Ваши подписки:", keyboard(buttons))


async def show_subscription_detail(message: Message, user_id: int, sub_id: int) -> None:
    with db() as conn:
        row = conn.execute(
            "select * from subscriptions where id = ? and telegram_id = ?",
            (sub_id, user_id),
        ).fetchone()
    if not row:
        await show_subscriptions(message, user_id)
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
            f"Ссылка подписки: {quote_code(row['sub_url'])}",
            f"Осталось трафика: {format_bytes(left)}",
            f"Действует до: {format_date(expire_time)}",
        ]
    )
    await send_photo(message, "subs", caption, subscription_action_keyboard(row["sub_url"]))


def validate_config() -> None:
    missing = [
        name
        for name, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "ADMIN_TELEGRAM_ID": ADMIN_TELEGRAM_ID,
            "HUI_USERNAME": HUI_USERNAME,
            "HUI_PASSWORD": HUI_PASSWORD,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Fill .env values: {', '.join(missing)}")
    for name, path in IMAGES.items():
        if not path.exists():
            raise RuntimeError(f"Image for {name} not found: {path}")


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="newcode", description="Создать код приглашения"),
        ]
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    validate_config()
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await set_commands(bot)
    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
