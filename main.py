"""
RefreshClip — Telegram-бот для уникализации видео перед публикацией
на YouTube, Instagram и TikTok.
Бот: @RefreshClipBot

Возможности:
  • новая версия видео: кадрирование, цвет, скорость, звук, зерно, очистка метаданных;
  • уровни изменений (мягко / стандарт / сильно) и до 3 копий за раз;
  • обязательная подписка на канал @ads_247_ru;
  • добровольные донаты в Telegram Stars (⭐);
  • современный интерфейс на inline-кнопках.

Используйте бот только для своих материалов или тех, на которые у вас есть права.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from processor import Options, ProcessingError, probe, uniqualize

# ════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ  (всё можно переопределить переменными окружения)
# ════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "RefreshClipBot").strip().lstrip("@")

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@ads_247_ru").strip()
if not CHANNEL_USERNAME.startswith("@"):
    CHANNEL_USERNAME = "@" + CHANNEL_USERNAME
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"

ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", CHANNEL_USERNAME).strip()

MAX_INPUT_BYTES = 20 * 1024 * 1024  # Bot API отдаёт ботам файлы только до 20 МБ
MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # боты могут отправлять до 50 МБ
MAX_DURATION = int(os.getenv("MAX_DURATION", "300"))  # сек.
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "1"))  # одновременных обработок
MAX_COPIES = 3
VIDEO_MAX_SIDE = int(os.getenv("VIDEO_MAX_SIDE", "1920"))  # на слабом тарифе поставьте 1280
FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "2"))
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "900"))

MAX_STARS = 10_000
DONATE_PRESETS = (10, 50, 100, 250, 500, 1000)

LEVELS = {1: ("Мягко", "🌿"), 2: ("Стандарт", "⚖️"), 3: ("Сильно", "🔥")}
TOGGLES = (
    ("color", "🎨 Цвет"),
    ("crop", "✂️ Кадр"),
    ("speed", "⏩ Скорость"),
    ("audio", "🔊 Звук"),
    ("noise", "🎞 Зерно"),
    ("flip", "🪞 Зеркало"),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("refreshclip")

# ════════════════════════════════════════════════════════════════
#  ТЕКСТЫ
# ════════════════════════════════════════════════════════════════
WELCOME = (
    "👋 <b>Привет, {name}!</b>\n\n"
    "Я <b>RefreshClip</b> — готовлю видео к публикации на <b>YouTube</b>, "
    "<b>Instagram</b> и <b>TikTok</b>: создаю новую уникальную версию файла.\n\n"
    "<b>Что я меняю</b>\n"
    "🎨 цвет · ✂️ кадр · ⏩ скорость · 🔊 звук · 🎞 зерно · 🧹 метаданные\n\n"
    "<b>Как это работает</b>\n"
    "1️⃣ Отправьте видео (до 20 МБ)\n"
    "2️⃣ Настройте уровень изменений\n"
    "3️⃣ Получите готовые копии ⚡\n\n"
    "⚠️ <i>Используйте только свои материалы или те, на которые у вас есть права.</i>"
)

HELP = (
    "📖 <b>Как пользоваться ботом</b>\n\n"
    "Пришлите видео — как обычное или как файл. Бот предложит настройки, после чего "
    "соберёт новую версию ролика.\n\n"
    "<b>Настройки</b>\n"
    "🌿 <b>Мягко</b> — почти незаметные изменения\n"
    "⚖️ <b>Стандарт</b> — оптимальный вариант\n"
    "🔥 <b>Сильно</b> — заметнее по цвету, кадру и скорости\n"
    "🪞 <b>Зеркало</b> — отражает кадр (текст в кадре тоже отразится)\n"
    "🔢 <b>Копии</b> — до 3 версий, у каждой свои параметры\n"
    "📁 <b>Файлом</b> — отправка без пересжатия Telegram\n\n"
    "<b>Ограничения</b>\n"
    "• Размер видео — до 20 МБ (лимит Telegram для ботов)\n"
    f"• Длительность — до {MAX_DURATION // 60} мин\n"
    "• Метаданные исходного файла удаляются всегда\n\n"
    "ℹ️ <i>Платформы сами решают, как оценивать контент, — результат не гарантирован. "
    "Используйте только свои материалы или те, на которые у вас есть права.</i>\n\n"
    "💛 Бот бесплатный. Если он полезен — можно поддержать проект звёздами ⭐"
)

DONATE_TEXT = (
    "⭐ <b>Поддержать проект</b>\n\n"
    "Бот полностью бесплатный, а донаты — <b>исключительно добровольные</b>. "
    "Они помогают оплачивать сервер и развивать бота 💛\n\n"
    "Выберите сумму в Telegram Stars или введите свою 👇"
)

CUSTOM_AMOUNT_TEXT = (
    "✏️ <b>Своя сумма</b>\n\n"
    f"Отправьте количество звёзд числом — от 1 до {MAX_STARS:,}.".replace(",", " ")
)

SUBSCRIBE_TEXT = (
    "🔐 <b>Один шаг до начала работы</b>\n\n"
    "Чтобы пользоваться ботом, подпишитесь на наш канал "
    f"<b>{html.escape(CHANNEL_USERNAME)}</b> — там новости и обновления.\n\n"
    "После подписки нажмите <b>«✅ Я подписался»</b> 👇"
)

SEND_VIDEO = (
    "🎬 <b>Пришлите видео</b>\n\n"
    "Отправьте ролик до 20 МБ — как видео или как файл.\n"
    "<i>Затем я предложу настройки уникализации.</i>"
)

SESSION_EXPIRED = "⌛ Сессия устарела — пришлите видео заново"


# ════════════════════════════════════════════════════════════════
#  СЕССИИ (настройки пользователя)
# ════════════════════════════════════════════════════════════════
@dataclass
class Session:
    file_id: str
    file_name: str
    size: int
    duration: int | None
    width: int | None
    height: int | None
    opts: Options = field(default_factory=Options)
    copies: int = 1
    as_document: bool = False


SESSIONS: dict[int, Session] = {}
ACTIVE_USERS: set[int] = set()
SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL)


def remember(user_id: int, session: Session) -> None:
    prev = SESSIONS.get(user_id)
    if prev:  # переносим предпочтения с прошлого видео
        session.opts, session.copies, session.as_document = prev.opts, prev.copies, prev.as_document
    SESSIONS[user_id] = session
    while len(SESSIONS) > 2000:
        SESSIONS.pop(next(iter(SESSIONS)))


# ════════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ И КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════════════
def fmt_duration(sec: float | int | None) -> str:
    if not sec:
        return "—"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_size(num: int) -> str:
    return f"{num / 1048576:.1f} МБ" if num >= 1048576 else f"{max(1, num // 1024)} КБ"


def settings_text(s: Session) -> str:
    name, icon = LEVELS[s.opts.level]
    dims = f"{s.width}×{s.height}" if s.width and s.height else "—"
    return (
        "🎬 <b>Видео получено</b>\n"
        f"📐 {dims}  ·  ⏱ {fmt_duration(s.duration)}  ·  💾 {fmt_size(s.size)}\n\n"
        "⚙️ <b>Настройте уникализацию</b>\n"
        f"Уровень изменений: {icon} <b>{name}</b>\n"
        "<i>🧹 Метаданные очищаются всегда.</i>\n\n"
        "Нажмите <b>«🚀 Уникализировать»</b>, когда будете готовы.\n"
        "<i>Нажимая кнопку, вы подтверждаете, что у вас есть права на это видео.</i>"
    )


def settings_kb(s: Session) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for level, (name, icon) in LEVELS.items():
        mark = "✅" if s.opts.level == level else icon
        kb.button(text=f"{mark} {name}", callback_data=f"lvl:{level}")
    for key, label in TOGGLES:
        mark = "✅" if getattr(s.opts, key) else "▫️"
        kb.button(text=f"{mark} {label}", callback_data=f"opt:{key}")
    kb.button(text=f"🔢 Копий: {s.copies}", callback_data="cnt")
    kb.button(text=f"📁 Файлом: {'да' if s.as_document else 'нет'}", callback_data="doc")
    kb.button(text="🚀 Уникализировать", callback_data="run")
    kb.button(text="✖️ Отмена", callback_data="cancel")
    kb.adjust(3, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Как это работает", callback_data="help")
    kb.button(text="⭐ Поддержать", callback_data="donate")
    kb.button(text="📢 Наш канал", url=CHANNEL_URL)
    kb.adjust(2, 1)
    return kb.as_markup()


def back_kb(target: str = "menu") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=target)
    return kb.as_markup()


def subscribe_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Подписаться на канал", url=CHANNEL_URL)
    kb.button(text="✅ Я подписался", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


def donate_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in DONATE_PRESETS:
        kb.button(text=f"⭐ {amount}", callback_data=f"pay:{amount}")
    kb.button(text="✏️ Своя сумма", callback_data="pay_custom")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(3, 3, 1, 1)
    return kb.as_markup()


def error_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ К настройкам", callback_data="again")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(2)
    return kb.as_markup()


def result_kb() -> InlineKeyboardMarkup:
    share_text = "Готовлю видео для YouTube, Instagram и TikTok прямо в Telegram 🎬"
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(f'https://t.me/{BOT_USERNAME}', safe='')}&text={quote(share_text)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё раз", callback_data="again")
    kb.button(text="⭐ Поддержать", callback_data="donate")
    kb.button(text="🔗 Поделиться ботом", url=share_url)
    kb.adjust(2, 1)
    return kb.as_markup()


def progress_text(pct: int, idx: int, total: int) -> str:
    filled = max(0, min(10, pct // 10))
    bar = "▰" * filled + "▱" * (10 - filled)
    part = f"  ·  копия {idx} из {total}" if total > 1 else ""
    return f"⚙️ <b>Уникализирую…</b>{part}\n\n<code>{bar}</code>  <b>{pct}%</b>"


# ════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════════
async def safe_edit(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            log.warning("edit_text failed: %s", e)


async def show_screen(
    call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Редактирует текущее сообщение, а если это не текст (например, видео) — шлёт новое."""
    msg = call.message
    if msg is None:
        return
    if getattr(msg, "text", None):
        await safe_edit(msg, text, reply_markup)
    else:
        await msg.answer(text, reply_markup=reply_markup)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    except TelegramAPIError as e:
        log.error(
            "Не удалось проверить подписку (бот должен быть администратором канала %s): %s",
            CHANNEL_USERNAME,
            e,
        )
        return False
    if member.status in (
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    ):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def _welcome_for(user_name: str | None) -> str:
    return WELCOME.format(name=html.escape(user_name or "друг"))


# ════════════════════════════════════════════════════════════════
#  MIDDLEWARE: обязательная подписка на канал
# ════════════════════════════════════════════════════════════════
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]
        user = data.get("event_from_user")

        if user is None or user.is_bot:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
            if event.data == "check_sub":
                return await handler(event, data)
        elif isinstance(event, Message):
            chat = event.chat
            if event.successful_payment:  # оплата уже прошла — обработать обязательно
                return await handler(event, data)
        else:
            return await handler(event, data)
        if chat is None or chat.type != ChatType.PRIVATE:
            return await handler(event, data)

        if await is_subscribed(bot, user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("🔐 Сначала подпишитесь на канал", show_alert=True)
            if event.message:
                await event.message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb())
        else:
            await event.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb())
        return None


# ════════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ════════════════════════════════════════════════════════════════
router = Router(name="main")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class DonateState(StatesGroup):
    custom_amount = State()


# ── Команды ──────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_welcome_for(message.from_user.first_name), reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=back_kb())


@router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(DONATE_TEXT, reply_markup=donate_kb())


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(
        "💬 <b>Поддержка по платежам</b>\n\n"
        f"Если возникла проблема с донатом, напишите нам: {html.escape(ADMIN_CONTACT)}\n"
        "Укажите, пожалуйста, сумму и время платежа."
    )


# ── Подписка и навигация ─────────────────────────────────────────
@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot) -> None:
    if await is_subscribed(bot, call.from_user.id):
        await call.answer("✅ Подписка подтверждена!")
        await show_screen(call, _welcome_for(call.from_user.first_name), main_kb())
    else:
        await call.answer("❌ Вы ещё не подписались на канал", show_alert=True)


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_screen(call, _welcome_for(call.from_user.first_name), main_kb())
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery) -> None:
    await show_screen(call, HELP, back_kb())
    await call.answer()


# ── Донаты в Telegram Stars ──────────────────────────────────────
async def send_donation_invoice(message: Message, amount: int) -> None:
    await message.answer_invoice(
        title="Поддержка проекта ⭐",
        description=f"Добровольный донат {amount} ⭐ на развитие бота. Спасибо, что вы с нами!",
        payload=f"donate:{amount}",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Донат", amount=amount)],
    )


@router.callback_query(F.data == "donate")
async def cb_donate(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_screen(call, DONATE_TEXT, donate_kb())
    await call.answer()


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def cb_pay(call: CallbackQuery) -> None:
    amount = int(call.data.split(":")[1])
    if not 1 <= amount <= MAX_STARS or call.message is None:
        await call.answer("Некорректная сумма", show_alert=True)
        return
    await send_donation_invoice(call.message, amount)
    await call.answer()


@router.callback_query(F.data == "pay_custom")
async def cb_pay_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DonateState.custom_amount)
    await show_screen(call, CUSTOM_AMOUNT_TEXT, back_kb("donate"))
    await call.answer()


@router.message(StateFilter(DonateState.custom_amount), F.text.regexp(r"^\s*\d{1,6}\s*$"))
async def custom_amount(message: Message, state: FSMContext) -> None:
    amount = int(message.text)
    if not 1 <= amount <= MAX_STARS:
        await message.answer(f"⚠️ Введите число от 1 до {MAX_STARS}.")
        return
    await state.clear()
    await send_donation_invoice(message, amount)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    payment = message.successful_payment
    log.info(
        "Stars donation: user=%s amount=%s %s charge_id=%s",
        message.from_user.id,
        payment.total_amount,
        payment.currency,
        payment.telegram_payment_charge_id,
    )
    await message.answer(
        "💛 <b>Спасибо за поддержку!</b>\n\n"
        f"Ваш донат — <b>{payment.total_amount} ⭐</b> — получен.\n"
        "Благодаря вам бот становится лучше!",
        reply_markup=back_kb(),
    )


# ── Приём видео ──────────────────────────────────────────────────
@router.message(F.video | F.animation | F.document)
async def on_video(message: Message) -> None:
    media = message.video or message.animation or message.document
    if media is None:
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("🎬 <b>Это не видео.</b>\n\n" + SEND_VIDEO, reply_markup=back_kb())
        return

    size = media.file_size or 0
    if size > MAX_INPUT_BYTES:
        await message.answer(
            f"📦 <b>Файл слишком большой</b> — {fmt_size(size)}.\n"
            "Telegram разрешает ботам принимать файлы только до <b>20 МБ</b>. "
            "Сожмите видео или отправьте покороче.",
            reply_markup=back_kb(),
        )
        return

    duration = getattr(media, "duration", None)
    if duration and duration > MAX_DURATION:
        await message.answer(
            f"⏱ <b>Видео слишком длинное</b> — {fmt_duration(duration)}.\n"
            f"Максимальная длительность — {MAX_DURATION // 60} мин.",
            reply_markup=back_kb(),
        )
        return

    session = Session(
        file_id=media.file_id,
        file_name=getattr(media, "file_name", None) or "video.mp4",
        size=size,
        duration=duration,
        width=getattr(media, "width", None),
        height=getattr(media, "height", None),
    )
    remember(message.from_user.id, session)
    await message.answer(settings_text(session), reply_markup=settings_kb(session))


# ── Настройки ────────────────────────────────────────────────────
async def _refresh_settings(call: CallbackQuery, s: Session) -> None:
    await show_screen(call, settings_text(s), settings_kb(s))
    await call.answer()


@router.callback_query(F.data.regexp(r"^lvl:[123]$"))
async def cb_level(call: CallbackQuery) -> None:
    s = SESSIONS.get(call.from_user.id)
    if not s:
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    s.opts.level = int(call.data.split(":")[1])
    await _refresh_settings(call, s)


@router.callback_query(F.data.startswith("opt:"))
async def cb_toggle(call: CallbackQuery) -> None:
    s = SESSIONS.get(call.from_user.id)
    key = call.data.split(":", 1)[1]
    if not s:
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    if key not in dict(TOGGLES):
        await call.answer()
        return
    setattr(s.opts, key, not getattr(s.opts, key))
    await _refresh_settings(call, s)


@router.callback_query(F.data == "cnt")
async def cb_copies(call: CallbackQuery) -> None:
    s = SESSIONS.get(call.from_user.id)
    if not s:
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    s.copies = s.copies % MAX_COPIES + 1
    await _refresh_settings(call, s)


@router.callback_query(F.data == "doc")
async def cb_document(call: CallbackQuery) -> None:
    s = SESSIONS.get(call.from_user.id)
    if not s:
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    s.as_document = not s.as_document
    await _refresh_settings(call, s)


@router.callback_query(F.data == "again")
async def cb_again(call: CallbackQuery) -> None:
    s = SESSIONS.get(call.from_user.id)
    if not s:
        await show_screen(call, SEND_VIDEO, back_kb())
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    await _refresh_settings(call, s)


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    SESSIONS.pop(call.from_user.id, None)
    await show_screen(call, "✖️ <b>Отменено.</b>\n\nОтправьте новое видео, когда будете готовы.", main_kb())
    await call.answer()


# ── Обработка ────────────────────────────────────────────────────
def _make_progress(status: Message, idx: int, total: int) -> Callable[[int], Awaitable[None]]:
    state = {"t": 0.0, "pct": -1}

    async def callback(pct: int) -> None:
        now = time.monotonic()
        if pct == state["pct"] or now - state["t"] < 3:
            return
        state["t"], state["pct"] = now, pct
        await safe_edit(status, progress_text(pct, idx, total))

    return callback


@router.callback_query(F.data == "run")
async def cb_run(call: CallbackQuery, bot: Bot) -> None:
    uid = call.from_user.id
    s = SESSIONS.get(uid)
    if not s or call.message is None:
        await call.answer(SESSION_EXPIRED, show_alert=True)
        return
    if uid in ACTIVE_USERS:
        await call.answer("⏳ Предыдущее видео ещё обрабатывается", show_alert=True)
        return
    await call.answer()
    await run_job(call.message, bot, uid, s)


async def run_job(status: Message, bot: Bot, uid: int, s: Session) -> None:
    ACTIVE_USERS.add(uid)
    chat_id = status.chat.id
    try:
        if SEMAPHORE.locked():
            await safe_edit(status, "🕐 <b>Вы в очереди</b>\n<i>Начну, как только освободится место…</i>")

        async with SEMAPHORE:
            await safe_edit(status, "📥 <b>Загружаю видео…</b>")
            with tempfile.TemporaryDirectory(prefix="refresh_") as tmp_name:
                tmp = Path(tmp_name)
                src = tmp / "input.mp4"
                try:
                    await bot.download(s.file_id, destination=src, timeout=120)
                except TelegramBadRequest as e:
                    log.warning("download failed: %s", e)
                    raise ProcessingError(
                        "📦 <b>Не удалось загрузить файл.</b>\n"
                        "Возможно, он больше 20 МБ или уже недоступен — отправьте видео заново."
                    ) from e

                info = await probe(src)
                if info.duration > MAX_DURATION:
                    raise ProcessingError(
                        f"⏱ <b>Видео слишком длинное.</b>\nМаксимальная длительность — {MAX_DURATION // 60} мин."
                    )

                level_name, level_icon = LEVELS[s.opts.level]
                stem = Path(s.file_name).stem[:40] or "video"

                for i in range(1, s.copies + 1):
                    dst = tmp / f"out_{i}.mp4"
                    await safe_edit(status, progress_text(0, i, s.copies))
                    async with ChatActionSender.upload_video(bot=bot, chat_id=chat_id):
                        plan = await uniqualize(
                            src,
                            dst,
                            info,
                            s.opts,
                            max_bytes=MAX_UPLOAD_BYTES,
                            max_side=VIDEO_MAX_SIDE,
                            threads=FFMPEG_THREADS,
                            timeout=FFMPEG_TIMEOUT,
                            on_progress=_make_progress(status, i, s.copies),
                        )
                        out = await probe(dst)
                        await safe_edit(status, "📤 <b>Отправляю…</b>")

                        title = f"Копия {i}/{s.copies}" if s.copies > 1 else "Готово"
                        caption = (
                            f"✨ <b>{title}</b>  ·  {level_icon} {level_name}\n"
                            f"{plan.summary}\n"
                            f"💾 {fmt_size(dst.stat().st_size)}  ·  🤖 @{BOT_USERNAME}"
                        )
                        markup = result_kb() if i == s.copies else None
                        filename = f"{stem}_refresh_{secrets.token_hex(3)}.mp4"
                        file = FSInputFile(dst, filename=filename)
                        if s.as_document:
                            await status.answer_document(document=file, caption=caption, reply_markup=markup)
                        else:
                            await status.answer_video(
                                video=file,
                                caption=caption,
                                duration=int(out.duration) or None,
                                width=out.width or None,
                                height=out.height or None,
                                supports_streaming=True,
                                reply_markup=markup,
                            )
        try:
            await status.delete()
        except TelegramAPIError:
            pass

    except ProcessingError as e:
        await safe_edit(status, e.user_message, error_kb())
    except TelegramAPIError:
        log.exception("Ошибка Telegram при отправке результата")
        await safe_edit(status, "😔 <b>Не удалось отправить видео.</b>\nПопробуйте ещё раз.", error_kb())
    except Exception:
        log.exception("Непредвиденная ошибка обработки")
        await safe_edit(status, "😔 <b>Что-то пошло не так.</b>\nПопробуйте ещё раз чуть позже.", error_kb())
    finally:
        ACTIVE_USERS.discard(uid)


# ── Всё остальное ────────────────────────────────────────────────
@router.message(StateFilter(DonateState.custom_amount))
async def custom_amount_invalid(message: Message) -> None:
    await message.answer("⚠️ Введите количество звёзд <b>числом</b>, например <code>75</code>.", reply_markup=back_kb("donate"))


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(SEND_VIDEO, reply_markup=back_kb())


# ════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════
async def on_startup(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=True)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="🚀 Запустить бота"),
            BotCommand(command="help", description="📖 Как пользоваться"),
            BotCommand(command="donate", description="⭐ Поддержать проект"),
        ]
    )
    await bot.set_my_short_description("🎬 Уникализирую видео для YouTube, Instagram и TikTok. Бесплатно!")
    await bot.set_my_description(
        "🎬 Готовлю видео к публикации на YouTube, Instagram и TikTok.\n\n"
        "Меняю кадр, цвет, скорость, звук и метаданные — получаете новую версию файла ⚡\n"
        "Бот бесплатный, донаты в Telegram Stars — добровольные ⭐"
    )

    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)

    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            log.warning("Бот НЕ администратор канала %s — проверка подписки работать не будет!", CHANNEL_USERNAME)
    except TelegramAPIError as e:
        log.warning("Нет доступа к каналу %s: %s. Добавьте бота в администраторы канала.", CHANNEL_USERNAME, e)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN (токен от @BotFather).")

    bot = Bot(
        token=BOT_TOKEN,
        session=AiohttpSession(timeout=300),  # отправка видео до 50 МБ может быть долгой
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher(storage=MemoryStorage())

    subscription = SubscriptionMiddleware()
    router.message.outer_middleware(subscription)
    router.callback_query.outer_middleware(subscription)

    dp.include_router(router)
    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
