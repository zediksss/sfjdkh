import asyncio
import html
import logging
import os
import random
import secrets
import sqlite3
import string
from contextlib import contextmanager, suppress
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
)
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_IDS_RAW = os.getenv("ADMIN_TELEGRAM_IDS", os.getenv("ADMIN_TELEGRAM_ID", ""))
HUI_API_BASE_URL = os.getenv("HUI_API_BASE_URL", "http://127.0.0.1:8081/hui").rstrip("/")
HUI_PUBLIC_BASE_URL = os.getenv("HUI_PUBLIC_BASE_URL", "https://keysforuuu.shop:8081").rstrip("/")
HUI_USERNAME = os.getenv("HUI_USERNAME", "")
HUI_PASSWORD = os.getenv("HUI_PASSWORD", "")
BOT_DB_PATH = os.getenv("BOT_DB_PATH", str(BASE_DIR / "bot.sqlite3"))
PLATEGA_API_BASE_URL = os.getenv("PLATEGA_API_BASE_URL", "https://app.platega.io").rstrip("/")
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "")
PLATEGA_PAYMENT_POLL_INTERVAL = int(os.getenv("PLATEGA_PAYMENT_POLL_INTERVAL", "20"))
SUBSCRIPTION_CHECK_INTERVAL = int(os.getenv("SUBSCRIPTION_CHECK_INTERVAL", "60"))

PASSWORD = "123123"
DEVICE_LIMIT = 3
MAX_SUBSCRIPTIONS_PER_USER = 5
QUOTA_BYTES = 1024**4
TRIAL_QUOTA_BYTES = 1024**3
TRIAL_DURATION_MINUTES = 30
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-06-01-28"
USER_AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-06-01-22"
SUPPORT_URL = "https://t.me/esenuskoritelsup"
PLAN_PRICES = {
    1: 69,
    2: 129,
    3: 189,
}
MIN_TOPUP_AMOUNT = 10

IMAGES = {
    "hello": BASE_DIR / "hello.png",
    "buy": BASE_DIR / "buysub.png",
    "payment": BASE_DIR / "oplati.png",
    "paid": BASE_DIR / "paid.png",
    "subs": BASE_DIR / "mysubs.png",
    "agreement": BASE_DIR / "soglas.png",
    "uved": BASE_DIR / "uved.png",
}

router = Router()


def parse_admin_ids(raw_value: str) -> set[int]:
    admin_ids: set[int] = set()
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            logging.warning("Skipping invalid admin ID from env: %s", part)
    return admin_ids


ADMIN_TELEGRAM_IDS = parse_admin_ids(ADMIN_TELEGRAM_IDS_RAW)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def now_ms() -> int:
    return int(now_utc().timestamp() * 1000)


def quote(text: str) -> str:
    return f"<blockquote>{html.escape(text)}</blockquote>"


def quote_code(text: str) -> str:
    return f"<blockquote><code>{html.escape(text)}</code></blockquote>"


def format_money(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_TELEGRAM_IDS


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(BOT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            create table if not exists users (
                telegram_id integer primary key,
                username text,
                invited integer not null default 1,
                accepted_terms integer not null default 0,
                balance real not null default 0,
                trial_claimed integer not null default 0,
                state text,
                created_at text not null
            )
            """
        )
        ensure_column(conn, "users", "accepted_terms", "integer not null default 0")
        ensure_column(conn, "users", "balance", "real not null default 0")
        ensure_column(conn, "users", "trial_claimed", "integer not null default 0")
        ensure_column(conn, "users", "state", "text")

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
                created_at text not null,
                auto_renew integer not null default 1,
                is_trial integer not null default 0,
                notified_day integer not null default 0,
                notified_expired integer not null default 0,
                active integer not null default 1,
                con_pass text
            )
            """
        )
        ensure_column(conn, "subscriptions", "auto_renew", "integer not null default 1")
        ensure_column(conn, "subscriptions", "is_trial", "integer not null default 0")
        ensure_column(conn, "subscriptions", "notified_day", "integer not null default 0")
        ensure_column(conn, "subscriptions", "notified_expired", "integer not null default 0")
        ensure_column(conn, "subscriptions", "active", "integer not null default 1")
        ensure_column(conn, "subscriptions", "con_pass", "text")

        conn.execute(
            """
            create table if not exists payments (
                transaction_id text primary key,
                telegram_id integer not null,
                purpose text not null default 'subscription',
                months integer not null default 0,
                promo_code text,
                amount real not null,
                currency text not null,
                payment_url text not null,
                status text not null,
                delivered integer not null default 0,
                subscription_id integer,
                last_error text,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        ensure_column(conn, "payments", "purpose", "text not null default 'subscription'")
        ensure_column(conn, "payments", "months", "integer not null default 0")
        ensure_column(conn, "payments", "promo_code", "text")
        ensure_column(conn, "payments", "delivered", "integer not null default 0")

        conn.execute(
            """
            create table if not exists promo_codes (
                code text primary key,
                discount_percent integer not null,
                uses_left integer not null,
                created_by integer not null,
                created_at text not null
            )
            """
        )

        conn.execute(
            """
            create table if not exists promo_usages (
                code text not null,
                telegram_id integer not null,
                used_at text not null,
                primary key (code, telegram_id)
            )
            """
        )


def remember_user(user_id: int, username: Optional[str], accepted_terms: Optional[bool] = None) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into users (telegram_id, username, invited, accepted_terms, created_at)
            values (?, ?, 1, ?, ?)
            on conflict(telegram_id) do update set
                username = excluded.username,
                invited = 1,
                accepted_terms = case
                    when excluded.accepted_terms = 1 then 1
                    else users.accepted_terms
                end
            """,
            (user_id, username or "", 1 if accepted_terms else 0, now_iso()),
        )


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("select * from users where telegram_id = ?", (user_id,)).fetchone()


def user_accepted_terms(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["accepted_terms"])


def get_user_balance(user_id: int) -> float:
    row = get_user(user_id)
    return float(row["balance"]) if row else 0.0


def add_user_balance(user_id: int, amount: float) -> None:
    with db() as conn:
        conn.execute("update users set balance = balance + ? where telegram_id = ?", (amount, user_id))


def consume_user_balance(user_id: int, amount: float) -> bool:
    with db() as conn:
        row = conn.execute("select balance from users where telegram_id = ?", (user_id,)).fetchone()
        if not row or float(row["balance"]) < amount:
            return False
        conn.execute("update users set balance = balance - ? where telegram_id = ?", (amount, user_id))
    return True


def set_user_state(user_id: int, state: Optional[str]) -> None:
    with db() as conn:
        conn.execute("update users set state = ? where telegram_id = ?", (state, user_id))


def get_user_state(user_id: int) -> Optional[str]:
    row = get_user(user_id)
    return str(row["state"]) if row and row["state"] else None


def extract_promo_from_state(state: Optional[str]) -> Optional[str]:
    if not state or not state.startswith("promo:"):
        return None
    promo_code = state.split(":", 1)[1].strip()
    return promo_code or None


def has_claimed_trial(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["trial_claimed"])


def mark_trial_claimed(user_id: int) -> None:
    with db() as conn:
        conn.execute("update users set trial_claimed = 1 where telegram_id = ?", (user_id,))


def normalize_promo_code(code: str) -> str:
    return code.strip().upper()


def create_promo(code: str, discount_percent: int, uses_left: int, created_by: int) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into promo_codes (code, discount_percent, uses_left, created_by, created_at)
            values (?, ?, ?, ?, ?)
            on conflict(code) do update set
                discount_percent = excluded.discount_percent,
                uses_left = excluded.uses_left,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (normalize_promo_code(code), discount_percent, uses_left, created_by, now_iso()),
        )


def delete_promo(code: str) -> bool:
    with db() as conn:
        cursor = conn.execute("delete from promo_codes where code = ?", (normalize_promo_code(code),))
    return cursor.rowcount > 0


def get_promo(code: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("select * from promo_codes where code = ?", (normalize_promo_code(code),)).fetchone()


def promo_available_for_user(code: str, telegram_id: int) -> Optional[sqlite3.Row]:
    promo = get_promo(code)
    if not promo or int(promo["uses_left"]) <= 0:
        return None
    with db() as conn:
        usage = conn.execute(
            "select 1 from promo_usages where code = ? and telegram_id = ?",
            (normalize_promo_code(code), telegram_id),
        ).fetchone()
    if usage:
        return None
    return promo


def consume_promo(code: str, telegram_id: int) -> bool:
    normalized = normalize_promo_code(code)
    with db() as conn:
        usage = conn.execute(
            "select 1 from promo_usages where code = ? and telegram_id = ?",
            (normalized, telegram_id),
        ).fetchone()
        if usage:
            return False
        promo = conn.execute(
            "select uses_left from promo_codes where code = ?",
            (normalized,),
        ).fetchone()
        if not promo or int(promo["uses_left"]) <= 0:
            return False
        conn.execute(
            "update promo_codes set uses_left = uses_left - 1 where code = ?",
            (normalized,),
        )
        conn.execute(
            "insert into promo_usages (code, telegram_id, used_at) values (?, ?, ?)",
            (normalized, telegram_id, now_iso()),
        )
    return True


def calc_discounted_price(base_price: float, discount_percent: int) -> float:
    discounted = round(base_price * (100 - discount_percent) / 100, 2)
    return max(discounted, 1.0)


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def legal_buttons() -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="📄 Политика конфиденциальности", url=PRIVACY_POLICY_URL),
            InlineKeyboardButton(text="📝 Пользовательское соглашение", url=USER_AGREEMENT_URL),
        ]
    ]


def main_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🛒 Купить подписку", "buy")],
            [button("🔑 Мои подписки", "subs"), button("👤 Профиль", "profile")],
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


def profile_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🎁 Протестировать сервис", "trial")],
            [button("💳 Пополнить баланс", "balance_topup")],
            [InlineKeyboardButton(text="🛟 Поддержка", url=SUPPORT_URL)],
            [button("🏠 Назад в меню", "menu")],
        ]
    )


def buy_keyboard() -> InlineKeyboardMarkup:
    return buy_keyboard_with_promo()


def buy_keyboard_with_promo(promo_code: Optional[str] = None) -> InlineKeyboardMarkup:
    promo_label = f"🏷️ Промокод: {promo_code}" if promo_code else "🏷️ У меня есть промокод"
    promo_action = "promo_applied" if promo_code else "promo_enter"
    return keyboard(
        [
            [
                button("1️⃣ 1 мес. • 69р", "term:1"),
                button("2️⃣ 2 мес. • 129р", "term:2"),
                button("3️⃣ 3 мес. • 189р", "term:3"),
            ],
            [button(promo_label, promo_action)],
            [button("🏠 Назад в меню", "menu")],
        ]
    )


def payment_link_keyboard(payment_url: str, transaction_id: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [InlineKeyboardButton(text="💸 Перейти к оплате", url=payment_url)],
            [button("✅ Я оплатил", f"paid:{transaction_id}")],
            [button("🏠 Назад в меню", "menu")],
        ]
    )


def topup_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("💵 10р", "topup:10"), button("💵 50р", "topup:50"), button("💵 100р", "topup:100")],
            [button("💵 300р", "topup:300"), button("⌨️ Ввести сумму", "topup_manual")],
            [button("👤 Назад в профиль", "profile")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return keyboard([[button("🏠 Вернуться в меню", "menu")]])


def subscription_action_keyboard(sub_id: int, sub_url: str, auto_renew: bool) -> InlineKeyboardMarkup:
    renew_label = "⏸️ Выключить автопродление" if auto_renew else "▶️ Включить автопродление"
    renew_action = "off" if auto_renew else "on"
    return keyboard(
        [
            [InlineKeyboardButton(text="🔗 Открыть подписку", url=sub_url)],
            [button(renew_label, f"renew:{sub_id}:{renew_action}")],
            [button("🗑️ Удалить подписку", f"delete_sub:{sub_id}")],
            [button("🔙 Назад к подпискам", "subs")],
        ]
    )


def delete_subscription_confirm_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ Подтвердить", f"delete_sub_confirm:{sub_id}")],
            [button("❌ Отменить", f"sub:{sub_id}")],
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


async def send_photo_to_chat(
    bot: Bot,
    chat_id: int,
    image: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile(IMAGES[image]),
        caption=caption or None,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def show_menu(message: Message) -> None:
    await send_photo(message, "hello", "", main_keyboard())


async def show_agreement(message: Message) -> None:
    await send_photo(message, "agreement", "", agreement_keyboard())


async def show_profile(message: Message, user_id: int) -> None:
    balance = get_user_balance(user_id)
    caption = "\n".join(
        [
            f"Ваш ID: <code>{user_id}</code>",
            f"Статус: {'админ' if is_admin(user_id) else 'пользователь'}",
            f"Баланс: <b>{format_money(balance)} руб.</b>",
        ]
    )
    await send_photo(message, "hello", caption, profile_keyboard())


def term_label(months: int) -> str:
    labels = {1: "1 месяц", 2: "2 месяца", 3: "3 месяца"}
    return labels.get(months, f"{months} мес.")


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

    async def create_subscription(
        self,
        telegram_id: int,
        months: int,
        quota_bytes: int = QUOTA_BYTES,
        duration_minutes: Optional[int] = None,
    ) -> dict[str, Any]:
        username = random_username()
        con_pass = random_secret(12)
        if duration_minutes is not None:
            expire = int((now_utc() + timedelta(minutes=duration_minutes)).timestamp() * 1000)
        else:
            expire = int((now_utc() + timedelta(days=30 * months)).timestamp() * 1000)
        remark = f"tg:{telegram_id}"
        await self.authed(
            "POST",
            "/account/saveAccount",
            json={
                "username": username,
                "pass": PASSWORD,
                "conPass": con_pass,
                "quota": quota_bytes,
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
            "quota": quota_bytes,
            "expire_time": expire,
            "con_pass": con_pass,
        }

    async def get_account(self, account_id: int) -> dict[str, Any]:
        return await self.authed("GET", "/account/getAccount", params={"id": account_id})

    async def update_account_expire(
        self,
        account_id: int,
        username: str,
        quota: int,
        expire_time: int,
        con_pass: Optional[str],
        remark: str,
    ) -> None:
        await self.authed(
            "POST",
            "/account/saveAccount",
            json={
                "id": account_id,
                "username": username,
                "pass": PASSWORD,
                "conPass": con_pass or random_secret(12),
                "quota": quota,
                "expireTime": expire_time,
                "deviceNo": DEVICE_LIMIT,
                "deleted": 0,
                "remark": remark[:32],
            },
        )

    async def delete_account(self, account_id: int) -> None:
        attempts = [
            ("POST", "/account/deleteAccount", {"json": {"id": account_id}}),
            ("GET", "/account/deleteAccount", {"params": {"id": account_id}}),
            ("POST", "/account/delAccount", {"json": {"id": account_id}}),
            ("GET", "/account/delAccount", {"params": {"id": account_id}}),
        ]
        errors: list[str] = []
        for method, path, kwargs in attempts:
            try:
                await self.authed(method, path, **kwargs)
                return
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        raise RuntimeError("; ".join(errors))


class PlategaApi:
    def __init__(self) -> None:
        self._headers = {
            "X-MerchantId": PLATEGA_MERCHANT_ID,
            "X-Secret": PLATEGA_SECRET,
            "Content-Type": "application/json",
        }

    async def create_payment_link(self, telegram_id: int, purpose: str, amount: float, months: int = 0) -> dict[str, Any]:
        description = f"{purpose} TgId:{telegram_id} UserId:{telegram_id}"
        payload_value = f"telegram_id={telegram_id};purpose={purpose};months={months};amount={amount}"
        payload = {
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": description,
            "payload": payload_value,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{PLATEGA_API_BASE_URL}/v2/transaction/process",
                headers=self._headers,
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        transaction_id = data.get("transactionId")
        payment_url = data.get("url") or data.get("redirect")
        status = data.get("status")
        if not transaction_id or not payment_url or not status:
            raise RuntimeError("invalid platega create payment response")
        return {
            "transaction_id": str(transaction_id),
            "payment_url": str(payment_url),
            "status": str(status),
        }

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{PLATEGA_API_BASE_URL}/transaction/{transaction_id}",
                headers=self._headers,
            )
        response.raise_for_status()
        return response.json()


hui = HuiApi()
platega = PlategaApi()


def random_username() -> str:
    return "vpn" + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))


def random_secret(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def store_subscription(
    telegram_id: int,
    item: dict[str, Any],
    months: int,
    is_trial: bool = False,
    auto_renew: bool = True,
) -> int:
    with db() as conn:
        cursor = conn.execute(
            """
            insert into subscriptions
                (telegram_id, account_id, username, sub_url, months, quota, expire_time, created_at, auto_renew, is_trial, active, con_pass)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                telegram_id,
                item["account_id"],
                item["username"],
                item["sub_url"],
                months,
                item["quota"],
                item["expire_time"],
                now_iso(),
                1 if auto_renew else 0,
                1 if is_trial else 0,
                item.get("con_pass"),
            ),
        )
    return int(cursor.lastrowid)


def get_subscription(sub_id: int, user_id: Optional[int] = None) -> Optional[sqlite3.Row]:
    with db() as conn:
        if user_id is None:
            return conn.execute("select * from subscriptions where id = ?", (sub_id,)).fetchone()
        return conn.execute(
            "select * from subscriptions where id = ? and telegram_id = ?",
            (sub_id, user_id),
        ).fetchone()


def subscription_count(telegram_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            """
            select count(*) as total
            from subscriptions
            where telegram_id = ? and active = 1 and expire_time > ?
            """,
            (telegram_id, now_ms()),
        ).fetchone()
    return int(row["total"]) if row else 0


def list_user_subscriptions(telegram_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            select *
            from subscriptions
            where telegram_id = ? and active = 1 and expire_time > ?
            order by id desc
            """,
            (telegram_id, now_ms()),
        ).fetchall()


def list_active_subscriptions() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "select * from subscriptions where active = 1 order by id asc"
        ).fetchall()


def update_subscription_runtime(sub_id: int, quota: int, expire_time: int) -> None:
    with db() as conn:
        conn.execute(
            """
            update subscriptions
            set quota = ?, expire_time = ?
            where id = ?
            """,
            (quota, expire_time, sub_id),
        )


def set_subscription_auto_renew(sub_id: int, enabled: bool) -> None:
    with db() as conn:
        conn.execute("update subscriptions set auto_renew = ? where id = ?", (1 if enabled else 0, sub_id))


def set_subscription_notified_day(sub_id: int, value: bool) -> None:
    with db() as conn:
        conn.execute("update subscriptions set notified_day = ? where id = ?", (1 if value else 0, sub_id))


def set_subscription_notified_expired(sub_id: int, value: bool) -> None:
    with db() as conn:
        conn.execute("update subscriptions set notified_expired = ? where id = ?", (1 if value else 0, sub_id))


def set_subscription_active(sub_id: int, active: bool) -> None:
    with db() as conn:
        conn.execute("update subscriptions set active = ? where id = ?", (1 if active else 0, sub_id))


def renew_subscription_record(sub_id: int, expire_time: int) -> None:
    with db() as conn:
        conn.execute(
            """
            update subscriptions
            set expire_time = ?, active = 1, notified_day = 0, notified_expired = 0
            where id = ?
            """,
            (expire_time, sub_id),
        )


def create_payment(
    transaction_id: str,
    telegram_id: int,
    purpose: str,
    amount: float,
    payment_url: str,
    status: str,
    months: int = 0,
    promo_code: Optional[str] = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into payments
                (transaction_id, telegram_id, purpose, months, promo_code, amount, currency, payment_url, status, delivered, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, 'RUB', ?, ?, 0, ?, ?)
            on conflict(transaction_id) do update set
                telegram_id = excluded.telegram_id,
                purpose = excluded.purpose,
                months = excluded.months,
                promo_code = excluded.promo_code,
                amount = excluded.amount,
                payment_url = excluded.payment_url,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (transaction_id, telegram_id, purpose, months, promo_code, amount, payment_url, status, now_iso(), now_iso()),
        )


def get_payment(transaction_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("select * from payments where transaction_id = ?", (transaction_id,)).fetchone()


def list_open_payments() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            select *
            from payments
            where delivered = 0 and status in ('PENDING', 'CONFIRMED')
            order by created_at asc
            """
        ).fetchall()


def mark_payment_status(transaction_id: str, status: str, last_error: Optional[str] = None) -> None:
    with db() as conn:
        conn.execute(
            """
            update payments
            set status = ?, last_error = ?, updated_at = ?
            where transaction_id = ?
            """,
            (status, last_error, now_iso(), transaction_id),
        )


def claim_payment_for_delivery(transaction_id: str) -> bool:
    with db() as conn:
        cursor = conn.execute(
            """
            update payments
            set status = 'PROCESSING', updated_at = ?
            where transaction_id = ? and delivered = 0 and status = 'CONFIRMED'
            """,
            (now_iso(), transaction_id),
        )
    return cursor.rowcount > 0


def complete_payment_delivery(transaction_id: str, subscription_id: Optional[int] = None) -> None:
    with db() as conn:
        conn.execute(
            """
            update payments
            set delivered = 1, status = 'CONFIRMED', subscription_id = ?, updated_at = ?, last_error = null
            where transaction_id = ?
            """,
            (subscription_id, now_iso(), transaction_id),
        )


def format_bytes(value: int) -> str:
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.2f} ТБ"


def format_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


def format_datetime(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y %H:%M")


def renewal_caption(row: sqlite3.Row) -> str:
    if row["is_trial"]:
        return "Автопродление: отключено"
    if row["auto_renew"]:
        return "Автопродление: включено (пополните баланс, чтобы подписка продлилась)"
    return "Автопродление: выключено"


async def create_subscription_from_payment(bot: Bot, payment: sqlite3.Row) -> bool:
    if subscription_count(payment["telegram_id"]) >= MAX_SUBSCRIPTIONS_PER_USER:
        mark_payment_status(
            payment["transaction_id"],
            "DELIVERY_BLOCKED",
            "payment confirmed, but user already has maximum subscriptions",
        )
        return False
    promo_code = str(payment["promo_code"]) if payment["promo_code"] else None
    if promo_code and not consume_promo(promo_code, int(payment["telegram_id"])):
        mark_payment_status(
            payment["transaction_id"],
            "DELIVERY_BLOCKED",
            "promo code is no longer available for this user",
        )
        await send_photo_to_chat(
            bot,
            int(payment["telegram_id"]),
            "payment",
            "Промокод больше недоступен для этого платежа. Купите подписку заново.",
            back_keyboard(),
        )
        return False
    item = await hui.create_subscription(payment["telegram_id"], int(payment["months"]))
    subscription_id = store_subscription(payment["telegram_id"], item, int(payment["months"]))
    complete_payment_delivery(payment["transaction_id"], subscription_id)
    caption = "Оплата подтверждена. Ваша подписка:\n" + quote_code(item["sub_url"])
    await send_photo_to_chat(
        bot,
        payment["telegram_id"],
        "paid",
        caption,
        subscription_action_keyboard(subscription_id, item["sub_url"], True),
    )
    return True


async def credit_balance_from_payment(bot: Bot, payment: sqlite3.Row) -> bool:
    add_user_balance(payment["telegram_id"], float(payment["amount"]))
    complete_payment_delivery(payment["transaction_id"])
    balance = get_user_balance(payment["telegram_id"])
    caption = (
        f"Баланс пополнен на <b>{format_money(payment['amount'])} руб.</b>\n"
        f"Текущий баланс: <b>{format_money(balance)} руб.</b>"
    )
    await send_photo_to_chat(bot, payment["telegram_id"], "paid", caption, profile_keyboard())
    return True


async def deliver_paid_item(bot: Bot, payment: sqlite3.Row) -> bool:
    if int(payment["delivered"]):
        return True
    if not claim_payment_for_delivery(payment["transaction_id"]):
        return False
    try:
        if payment["purpose"] == "balance_topup":
            return await credit_balance_from_payment(bot, payment)
        return await create_subscription_from_payment(bot, payment)
    except Exception as exc:
        logging.exception("failed to deliver payment %s", payment["transaction_id"])
        mark_payment_status(payment["transaction_id"], "CONFIRMED", str(exc))
        return False


async def refresh_payment_status(bot: Bot, transaction_id: str) -> str:
    payment = get_payment(transaction_id)
    if not payment:
        return "NOT_FOUND"
    if int(payment["delivered"]):
        return "CONFIRMED"
    if payment["status"] == "CONFIRMED":
        await deliver_paid_item(bot, payment)
        return "CONFIRMED"
    try:
        transaction = await platega.get_transaction(transaction_id)
    except Exception as exc:
        logging.exception("failed to fetch payment status for %s", transaction_id)
        mark_payment_status(transaction_id, str(payment["status"]), str(exc))
        return str(payment["status"])
    status = str(transaction.get("status") or payment["status"])
    mark_payment_status(transaction_id, status)
    if status == "CONFIRMED":
        latest_payment = get_payment(transaction_id)
        if latest_payment:
            await deliver_paid_item(bot, latest_payment)
    return status


async def payment_polling_loop(bot: Bot) -> None:
    while True:
        try:
            for payment in list_open_payments():
                await refresh_payment_status(bot, payment["transaction_id"])
        except Exception:
            logging.exception("payment polling loop failed")
        await asyncio.sleep(PLATEGA_PAYMENT_POLL_INTERVAL)


async def try_auto_renew_subscription(bot: Bot, row: sqlite3.Row) -> bool:
    if row["is_trial"] or not row["auto_renew"]:
        return False
    price = PLAN_PRICES.get(int(row["months"]))
    if not price:
        return False
    if get_user_balance(row["telegram_id"]) < price:
        return False
    new_expire = row["expire_time"] + int(timedelta(days=30 * int(row["months"])).total_seconds() * 1000)
    try:
        await hui.update_account_expire(
            account_id=int(row["account_id"]),
            username=str(row["username"]),
            quota=int(row["quota"]),
            expire_time=new_expire,
            con_pass=row["con_pass"],
            remark=f"tg:{row['telegram_id']}",
        )
    except Exception:
        logging.exception("failed to auto renew subscription %s", row["id"])
        return False
    if not consume_user_balance(int(row["telegram_id"]), float(price)):
        return False
    renew_subscription_record(int(row["id"]), new_expire)
    balance = get_user_balance(int(row["telegram_id"]))
    caption = "\n".join(
        [
            f"Подписка <b>{html.escape(str(row['username']))}</b> автоматически продлена.",
            f"С баланса списано <b>{format_money(price)} руб.</b>",
            f"Новый срок: <b>{format_date(new_expire)}</b>",
            f"Остаток баланса: <b>{format_money(balance)} руб.</b>",
        ]
    )
    await send_photo_to_chat(bot, int(row["telegram_id"]), "paid", caption, profile_keyboard())
    return True


async def process_subscription_events(bot: Bot) -> None:
    current_ms = now_ms()
    one_day_ms = 24 * 60 * 60 * 1000
    for row in list_active_subscriptions():
        expire_time = int(row["expire_time"])
        if 0 < expire_time - current_ms <= one_day_ms and not row["notified_day"]:
            caption = (
                f"Ваша подписка <b>{html.escape(str(row['username']))}</b> закончится через день! "
                "Не забудьте продлить ее, чтоб быть на связи."
            )
            await send_photo_to_chat(bot, int(row["telegram_id"]), "uved", caption, subscription_action_keyboard(int(row["id"]), str(row["sub_url"]), bool(row["auto_renew"])))
            set_subscription_notified_day(int(row["id"]), True)
        if current_ms < expire_time:
            continue
        if await try_auto_renew_subscription(bot, row):
            continue
        if not row["notified_expired"]:
            caption = (
                f"Ваша подписка <b>{html.escape(str(row['username']))}</b> истекла, надеемся, вам все понравилось! "
                "Вы можете купить новую подписку в меню."
            )
            await send_photo_to_chat(bot, int(row["telegram_id"]), "uved", caption, back_keyboard())
            set_subscription_notified_expired(int(row["id"]), True)
        set_subscription_active(int(row["id"]), False)


async def subscription_maintenance_loop(bot: Bot) -> None:
    while True:
        try:
            await process_subscription_events(bot)
        except Exception:
            logging.exception("subscription maintenance loop failed")
        await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL)


@router.message(Command("start"))
async def start(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    remember_user(user.id, user.username)
    if user_accepted_terms(user.id):
        await show_menu(message)
        return
    await show_agreement(message)


@router.message(Command("newpromo"))
async def new_promo(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    args = (command.args or "").split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Формат: /newpromo ПРОМО процентскидки колвоиспользований")
        return
    code = normalize_promo_code(args[0])
    discount = int(args[1])
    uses = int(args[2])
    if not code or discount <= 0 or discount >= 100 or uses <= 0:
        await message.answer("Проверьте данные: скидка 1-99, использования больше 0.")
        return
    create_promo(code, discount, uses, user.id)
    await message.answer(f"Промокод {html.escape(code)} создан. Скидка: {discount}%. Использований: {uses}.")


@router.message(Command("delpromo"))
async def del_promo(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    code = normalize_promo_code(command.args or "")
    if not code:
        await message.answer("Формат: /delpromo ПРОМО")
        return
    if delete_promo(code):
        await message.answer(f"Промокод {html.escape(code)} удалён.")
    else:
        await message.answer("Промокод не найден.")


async def handle_topup_amount_input(message: Message, user_id: int) -> None:
    raw_amount = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(raw_amount)
    except ValueError:
        await message.answer(f"Введите сумму числом. Минимум {MIN_TOPUP_AMOUNT} рублей.")
        return
    if amount < MIN_TOPUP_AMOUNT:
        await message.answer(f"Минимальная сумма пополнения {MIN_TOPUP_AMOUNT} рублей.")
        return
    set_user_state(user_id, None)
    try:
        payment = await platega.create_payment_link(user_id, "balance_topup", amount)
        create_payment(
            payment["transaction_id"],
            user_id,
            "balance_topup",
            amount,
            payment["payment_url"],
            payment["status"],
        )
    except Exception:
        logging.exception("failed to create topup link")
        await send_photo(message, "payment", "Не получилось создать ссылку на оплату пополнения.", profile_keyboard())
        return
    caption = "\n".join(
        [
            f"Пополнение баланса на <b>{format_money(amount)} руб.</b>",
            "Ссылка на оплату:",
            html.escape(payment["payment_url"]),
            "",
            "После оплаты нажмите «Я оплатил». Деньги придут на баланс автоматически.",
        ]
    )
    await send_photo(message, "payment", caption, payment_link_keyboard(payment["payment_url"], payment["transaction_id"]))


async def handle_promo_input(message: Message, user_id: int) -> None:
    promo_code = normalize_promo_code(message.text or "")
    set_user_state(user_id, None)
    promo = promo_available_for_user(promo_code, user_id)
    if not promo:
        await message.answer("Промокод не найден")
        await show_buy_screen(message)
        return
    set_user_state(user_id, f"promo:{promo_code}")
    await show_buy_screen_with_promo(message, promo_code)


@router.message(F.text & ~F.text.startswith("/"))
async def text_message(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    remember_user(user.id, user.username)
    if not user_accepted_terms(user.id):
        await show_agreement(message)
        return
    state = get_user_state(user.id)
    if state == "awaiting_topup_amount":
        await handle_topup_amount_input(message, user.id)
        return
    if state == "awaiting_promo_code":
        await handle_promo_input(message, user.id)
        return
    await show_menu(message)


async def show_buy_screen(message: Message) -> None:
    await show_buy_screen_with_promo(message, None)


async def show_buy_screen_with_promo(message: Message, promo_code: Optional[str]) -> None:
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
    viewer_id = message.from_user.id if message.from_user else message.chat.id
    if promo_code:
        promo = promo_available_for_user(promo_code, viewer_id)
        if promo:
            caption += "\n\n" + f"Промокод <b>{html.escape(promo_code)}</b> активирован. Скидка: <b>{int(promo['discount_percent'])}%</b>."
    await send_photo(message, "buy", caption, buy_keyboard_with_promo(promo_code))


async def show_subscription_detail(message: Message, user_id: int, sub_id: int) -> None:
    row = get_subscription(sub_id, user_id)
    if not row or not row["active"] or int(row["expire_time"]) <= now_ms():
        await show_subscriptions(message, user_id)
        return
    try:
        account = await hui.get_account(int(row["account_id"]))
        used = int(account.get("download", 0)) + int(account.get("upload", 0))
        quota = int(account.get("quota", row["quota"]))
        expire_time = int(account.get("expireTime", row["expire_time"]))
        update_subscription_runtime(int(row["id"]), quota, expire_time)
        row = get_subscription(sub_id, user_id) or row
    except Exception:
        logging.exception("failed to refresh account")
        used = 0
        quota = int(row["quota"])
        expire_time = int(row["expire_time"])
    left = max(quota - used, 0)
    caption = "\n".join(
        [
            f"Подписка: {html.escape(str(row['username']))}",
            f"Ссылка подписки: {quote_code(str(row['sub_url']))}",
            renewal_caption(row),
            f"Осталось трафика: {format_bytes(left)}",
            f"Действует до: {format_datetime(expire_time)}",
        ]
    )
    await send_photo(
        message,
        "subs",
        caption,
        subscription_action_keyboard(int(row["id"]), str(row["sub_url"]), bool(row["auto_renew"])),
    )


async def show_subscriptions(message: Message, user_id: int) -> None:
    rows = list_user_subscriptions(user_id)
    if not rows:
        await send_photo(message, "subs", "У вас пока нет подписок.", back_keyboard())
        return
    buttons = [[button(f"🔑 {row['username']} · {row['months']} мес.", f"sub:{row['id']}")] for row in rows]
    buttons.append([button("🏠 Назад в меню", "menu")])
    await send_photo(message, "subs", "Ваши подписки:", keyboard(buttons))


@router.callback_query()
async def callback(query: CallbackQuery) -> None:
    message = get_callback_message(query)
    if not message:
        await query.answer()
        return

    user = query.from_user
    remember_user(user.id, user.username)
    data = query.data or ""

    if data == "accept_terms":
        remember_user(user.id, user.username, accepted_terms=True)
        await query.answer()
        await delete_message(message)
        await show_menu(message)
        return

    if not user_accepted_terms(user.id):
        await query.answer()
        await delete_message(message)
        await show_agreement(message)
        return

    if data == "menu":
        set_user_state(user.id, None)
        await query.answer()
        await delete_message(message)
        await show_menu(message)
        return

    if data == "profile":
        set_user_state(user.id, None)
        await query.answer()
        await delete_message(message)
        await show_profile(message, user.id)
        return

    if data == "balance_topup":
        set_user_state(user.id, None)
        await query.answer()
        await delete_message(message)
        caption = "\n".join(
            [
                f"Ваш баланс: <b>{format_money(get_user_balance(user.id))} руб.</b>",
                f"Выберите сумму пополнения или введите свою. Минимум {MIN_TOPUP_AMOUNT} рублей.",
            ]
        )
        await send_photo(message, "payment", caption, topup_keyboard())
        return

    if data == "topup_manual":
        set_user_state(user.id, "awaiting_topup_amount")
        await query.answer()
        await message.answer(f"Введите сумму пополнения числом. Минимум {MIN_TOPUP_AMOUNT} рублей.")
        return

    if data.startswith("topup:"):
        try:
            amount = float(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная сумма.", show_alert=True)
            return
        await query.answer()
        try:
            payment = await platega.create_payment_link(user.id, "balance_topup", amount)
            create_payment(
                payment["transaction_id"],
                user.id,
                "balance_topup",
                amount,
                payment["payment_url"],
                payment["status"],
            )
        except Exception:
            logging.exception("failed to create topup link")
            await query.answer("Не получилось создать ссылку на оплату.", show_alert=True)
            return
        await delete_message(message)
        caption = "\n".join(
            [
                f"Пополнение баланса на <b>{format_money(amount)} руб.</b>",
                "Ссылка на оплату:",
                html.escape(payment["payment_url"]),
                "",
                "После оплаты нажмите «Я оплатил». Деньги придут на баланс автоматически.",
            ]
        )
        await send_photo(message, "payment", caption, payment_link_keyboard(payment["payment_url"], payment["transaction_id"]))
        return

    if data == "trial":
        if has_claimed_trial(user.id):
            await query.answer("Тестовый ключ уже был получен на этот аккаунт.", show_alert=True)
            return
        if subscription_count(user.id) >= MAX_SUBSCRIPTIONS_PER_USER:
            await query.answer("У вас уже есть 5/5 подписок", show_alert=True)
            return
        await query.answer()
        try:
            item = await hui.create_subscription(
                telegram_id=user.id,
                months=0,
                quota_bytes=TRIAL_QUOTA_BYTES,
                duration_minutes=TRIAL_DURATION_MINUTES,
            )
            subscription_id = store_subscription(user.id, item, months=0, is_trial=True, auto_renew=False)
            mark_trial_claimed(user.id)
        except Exception:
            logging.exception("failed to create trial subscription")
            await query.answer("Не получилось выдать тестовый ключ.", show_alert=True)
            return
        await delete_message(message)
        caption = "\n".join(
            [
                "Тестовый доступ активирован.",
                "Срок: 30 минут",
                "Трафик: 1 ГБ",
                quote_code(item["sub_url"]),
            ]
        )
        await send_photo(
            message,
            "paid",
            caption,
            subscription_action_keyboard(subscription_id, item["sub_url"], False),
        )
        return

    if data == "buy":
        if subscription_count(user.id) >= MAX_SUBSCRIPTIONS_PER_USER:
            await query.answer("У вас уже есть 5/5 подписок", show_alert=True)
            return
        await query.answer()
        await delete_message(message)
        await show_buy_screen_with_promo(message, extract_promo_from_state(get_user_state(user.id)))
        return

    if data == "promo_enter":
        set_user_state(user.id, "awaiting_promo_code")
        await query.answer()
        await message.answer("Введите промокод.")
        return

    if data == "promo_applied":
        promo_code = extract_promo_from_state(get_user_state(user.id))
        if promo_code:
            await query.answer(f"Промокод: {promo_code}")
        else:
            await query.answer()
        return

    if data.startswith("term:"):
        try:
            months = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некорректный срок подписки.", show_alert=True)
            return
        price = PLAN_PRICES.get(months)
        if price is None:
            await query.answer("Тариф не найден.", show_alert=True)
            return
        if subscription_count(user.id) >= MAX_SUBSCRIPTIONS_PER_USER:
            await query.answer("У вас уже есть 5/5 подписок", show_alert=True)
            return
        promo_code = extract_promo_from_state(get_user_state(user.id))
        promo = promo_available_for_user(promo_code, user.id) if promo_code else None
        final_price = float(price)
        discount_percent = 0
        if promo:
            discount_percent = int(promo["discount_percent"])
            final_price = calc_discounted_price(float(price), discount_percent)
        await query.answer()
        try:
            payment = await platega.create_payment_link(user.id, "subscription", final_price, months)
            create_payment(
                payment["transaction_id"],
                user.id,
                "subscription",
                final_price,
                payment["payment_url"],
                payment["status"],
                months,
                promo_code=promo_code if promo else None,
            )
        except Exception:
            logging.exception("failed to create payment link")
            await send_photo(
                message,
                "payment",
                "Не получилось создать ссылку на оплату. Попробуйте ещё раз чуть позже.",
                back_keyboard(),
            )
            return
        await delete_message(message)
        set_user_state(user.id, None)
        caption = "\n".join(
            [
                f"Ваш выбор - ускоритель на {term_label(months)}",
                f"Стоимость: {format_money(final_price)} руб. ({round(final_price / months)}р/мес.)",
                "",
                "Ссылка на оплату:",
                html.escape(payment["payment_url"]),
                "",
                "После оплаты нажмите «Я оплатил». Если вы уйдёте в меню, платёж всё равно проверится автоматически.",
            ]
        )
        if promo and discount_percent:
            caption = "\n".join(
                [
                    f"Промокод: <b>{html.escape(promo_code or '')}</b>",
                    f"Скидка: <b>{discount_percent}%</b>",
                    f"Старая цена: {price} руб.",
                    caption,
                ]
            )
        await send_photo(
            message,
            "payment",
            caption,
            payment_link_keyboard(payment["payment_url"], payment["transaction_id"]),
        )
        return

    if data.startswith("paid:"):
        transaction_id = data.split(":", 1)[1].strip()
        if not transaction_id:
            await query.answer("Не найден платёж для проверки.", show_alert=True)
            return
        await query.answer("Проверяем ваш платеж...")
        await refresh_payment_status(query.bot, transaction_id)
        return

    if data == "subs":
        await query.answer()
        await delete_message(message)
        await show_subscriptions(message, user.id)
        return

    if data.startswith("sub:"):
        try:
            sub_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Подписка не найдена.", show_alert=True)
            return
        await query.answer()
        await delete_message(message)
        await show_subscription_detail(message, user.id, sub_id)
        return

    if data.startswith("renew:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Некорректное действие.", show_alert=True)
            return
        sub_id = int(parts[1])
        enabled = parts[2] == "on"
        row = get_subscription(sub_id, user.id)
        if not row:
            await query.answer("Подписка не найдена.", show_alert=True)
            return
        if row["is_trial"]:
            await query.answer("Для тестового ключа автопродление недоступно.", show_alert=True)
            return
        set_subscription_auto_renew(sub_id, enabled)
        await query.answer("Автопродление обновлено.")
        await delete_message(message)
        await show_subscription_detail(message, user.id, sub_id)
        return

    if data.startswith("delete_sub:") and not data.startswith("delete_sub_confirm:"):
        sub_id = int(data.split(":", 1)[1])
        row = get_subscription(sub_id, user.id)
        if not row:
            await query.answer("Подписка не найдена.", show_alert=True)
            return
        await query.answer()
        await delete_message(message)
        caption = (
            "Точно ли хотите удалить ключ?\n"
            "Деньги за остаток НЕ вернутся."
        )
        await send_photo(message, "subs", caption, delete_subscription_confirm_keyboard(sub_id))
        return

    if data.startswith("delete_sub_confirm:"):
        sub_id = int(data.split(":", 1)[1])
        row = get_subscription(sub_id, user.id)
        if not row:
            await query.answer("Подписка не найдена.", show_alert=True)
            return
        await query.answer()
        try:
            await hui.delete_account(int(row["account_id"]))
        except Exception:
            logging.exception("failed to delete subscription from panel")
            await query.answer("Не получилось удалить ключ с панели.", show_alert=True)
            return
        set_subscription_active(sub_id, False)
        await delete_message(message)
        await show_subscriptions(message, user.id)
        return

    await query.answer()


def validate_config() -> None:
    missing = [
        name
        for name, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "ADMIN_TELEGRAM_IDS": ADMIN_TELEGRAM_IDS_RAW,
            "HUI_USERNAME": HUI_USERNAME,
            "HUI_PASSWORD": HUI_PASSWORD,
            "PLATEGA_MERCHANT_ID": PLATEGA_MERCHANT_ID,
            "PLATEGA_SECRET": PLATEGA_SECRET,
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
            BotCommand(command="newpromo", description="Создать промокод"),
            BotCommand(command="delpromo", description="Удалить промокод"),
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
    payment_task = asyncio.create_task(payment_polling_loop(bot))
    subscription_task = asyncio.create_task(subscription_maintenance_loop(bot))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        payment_task.cancel()
        subscription_task.cancel()
        with suppress(asyncio.CancelledError):
            await payment_task
        with suppress(asyncio.CancelledError):
            await subscription_task


if __name__ == "__main__":
    asyncio.run(main())
