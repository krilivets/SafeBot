import asyncio
import html
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from collections import OrderedDict
from io import BytesIO
from uuid import uuid4

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BusinessConnection,
    BusinessMessagesDeleted,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramAPIError,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в .env")


ARCHIVE_CHAT_ID_FILE = Path(os.getenv("ARCHIVE_CHAT_ID_FILE", "archive_chat_id.txt"))
DB_PATH = Path(os.getenv("DB_PATH", "bot_admin.db"))

# Код для входа в админ-панель.
# Можно поменять в .env:
# ADMIN_CODE=NelcyRu4235
ADMIN_CODE = os.getenv("ADMIN_CODE", "NelcyRu4235")

# Канал, на который пользователь должен быть подписан для использования бота.
# Можно поменять в .env:
# REQUIRED_CHANNEL=@nelcyempire
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@nelcyempire")
REQUIRED_CHANNEL_URL = "https://t.me/" + REQUIRED_CHANNEL.lstrip("@")


def parse_admin_ids():
    """
    В .env можно указать:
    ADMIN_IDS=123456789,987654321

    Это самый безопасный вариант: админка будет открываться только твоим ID
    или по коду, который указан в ADMIN_CODE.
    """
    raw = os.getenv("ADMIN_IDS", "").replace(" ", "")
    result = set()

    for item in raw.split(","):
        if item and item.lstrip("-").isdigit():
            result.add(int(item))

    return result


ADMIN_IDS = parse_admin_ids()
ADMIN_SESSIONS = set()
ADMIN_STATE = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MAX_CACHE_SIZE = 5000
message_cache = OrderedDict()

# Ключи сообщений, которые были написаны владельцем Business-аккаунта.
# Они нужны, чтобы при удалении своих сообщений бот НЕ присылал их обратно тебе
# и НЕ писал "От: неизвестно".
ignored_own_message_keys = OrderedDict()
MAX_IGNORED_OWN_KEYS = 5000

# Архив любых сообщений, которые бот видит через Telegram Business.
# Файлы сохраняются локально в папку saved_messages_media.
SAVE_ALL_MESSAGES = os.getenv("SAVE_ALL_MESSAGES", "1").strip().lower() not in {"0", "false", "no", "off"}
ALL_MESSAGES_MEDIA_DIR = Path(os.getenv("ALL_MESSAGES_MEDIA_DIR", "saved_messages_media"))
MAX_SAVE_MEDIA_MB = int(os.getenv("MAX_SAVE_MEDIA_MB", "45"))


def now_time():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        username TEXT,
        first_seen TEXT,
        last_seen TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS deleted_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_user_id INTEGER,
        source_name TEXT,
        source_username TEXT,
        chat_id INTEGER,
        chat_name TEXT,
        content_type TEXT,
        text TEXT,
        deleted_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS all_message_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_connection_id TEXT,
        business_message_id INTEGER,
        event_type TEXT,
        source_user_id INTEGER,
        source_name TEXT,
        source_username TEXT,
        chat_id INTEGER,
        chat_name TEXT,
        content_type TEXT,
        text TEXT,
        media_file_id TEXT,
        media_file_path TEXT,
        media_filename TEXT,
        saved_at TEXT
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_all_message_logs_sender
    ON all_message_logs(source_user_id, source_username)
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_all_message_logs_chat
    ON all_message_logs(chat_id)
    """)

    # Если раньше ты уже писал /start, но пользователя не было в базе,
    # добавляем сохранённый архивный chat_id как получателя для broadcast.
    try:
        archive_chat_id = get_archive_chat_id()
        if archive_chat_id:
            current_time = now_time()
            cur.execute("""
            INSERT OR IGNORE INTO users (user_id, full_name, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """, (archive_chat_id, "Архивный чат", None, current_time, current_time))
    except Exception as e:
        print("Не смог добавить archive_chat_id в users:", e)

    con.commit()
    con.close()


def is_admin(user_id: int | None):
    if not user_id:
        return False

    return user_id in ADMIN_IDS or user_id in ADMIN_SESSIONS


def remember_bot_user(user):
    """
    Сохраняет пользователей, которые писали боту в личку.
    Именно этим людям потом можно делать broadcast.
    """
    if not user:
        return

    con = db_connect()
    cur = con.cursor()

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
    exists = cur.fetchone()

    full_name = user.full_name or str(user.id)
    username = user.username
    current_time = now_time()

    if exists:
        cur.execute("""
        UPDATE users
        SET full_name = ?, username = ?, last_seen = ?
        WHERE user_id = ?
        """, (full_name, username, current_time, user.id))
    else:
        cur.execute("""
        INSERT INTO users (user_id, full_name, username, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        """, (user.id, full_name, username, current_time, current_time))

    con.commit()
    con.close()


def save_deleted_log(data: dict | None, event_chat=None):
    """
    Сохраняет удалённое сообщение в локальную SQLite-базу.
    В админке будут видны именно эти сохранённые записи.
    """
    if data:
        text = data.get("text") or data.get("caption") or ""
        if not text:
            text = "Без текста / медиафайл"

        if len(text) > 3500:
            text = text[:3500] + "\n…[обрезано]"

        source_user_id = data.get("from_user_id")
        source_name = data.get("from")
        source_username = data.get("from_username")
        chat_id = data.get("chat_id")
        chat_name = data.get("chat_name")
        content_type = data.get("type")
    else:
        text = "Я не успел запомнить это сообщение или бот был перезапущен."
        source_user_id = None
        source_name = "Неизвестно"
        source_username = None
        chat_id = getattr(event_chat, "id", None)
        chat_name = get_chat_name(event_chat)
        content_type = "unknown"

    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    INSERT INTO deleted_logs
    (source_user_id, source_name, source_username, chat_id, chat_name, content_type, text, deleted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source_user_id,
        source_name or "Неизвестно",
        source_username,
        chat_id,
        chat_name or "Неизвестно",
        content_type or "unknown",
        text,
        now_time(),
    ))

    con.commit()
    con.close()


def get_archive_chat_id():
    archive_chat_id = os.getenv("ARCHIVE_CHAT_ID")

    if archive_chat_id:
        return int(archive_chat_id)

    if ARCHIVE_CHAT_ID_FILE.exists():
        text = ARCHIVE_CHAT_ID_FILE.read_text(encoding="utf-8").strip()
        if text:
            return int(text)

    return None


def save_archive_chat_id(chat_id: int):
    ARCHIVE_CHAT_ID_FILE.write_text(str(chat_id), encoding="utf-8")


def parse_int_ids(raw: str):
    result = set()

    for item in (raw or "").replace(" ", "").split(","):
        if item and item.lstrip("-").isdigit():
            result.add(int(item))

    return result


def get_ignored_sender_ids():
    """
    Чьи удалённые сообщения НЕ надо отправлять в архив.

    По умолчанию сюда попадает archive_chat_id — то есть твой личный Telegram ID,
    если ты написал /start основному боту с главного аккаунта.

    Если нужно явно указать ID, можно добавить в .env:
    IGNORE_USER_IDS=123456789,987654321
    """
    result = set()

    archive_chat_id = get_archive_chat_id()
    if archive_chat_id:
        result.add(int(archive_chat_id))

    result.update(parse_int_ids(os.getenv("IGNORE_USER_IDS", "")))

    return result


def should_ignore_sender(source_user_id):
    if source_user_id is None:
        return False

    try:
        return int(source_user_id) in get_ignored_sender_ids()
    except (TypeError, ValueError):
        return False


def remember_ignored_own_message_key(key: str):
    ignored_own_message_keys[key] = True
    ignored_own_message_keys.move_to_end(key)

    while len(ignored_own_message_keys) > MAX_IGNORED_OWN_KEYS:
        ignored_own_message_keys.popitem(last=False)


def was_ignored_own_message_key(key: str):
    return key in ignored_own_message_keys


def forget_ignored_own_message_key(key: str):
    ignored_own_message_keys.pop(key, None)


def get_user_name(user):
    if not user:
        return "Неизвестно"

    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    if user.username:
        return f"{full_name} (@{user.username})"

    return full_name or str(user.id)


def get_chat_name(chat):
    if not chat:
        return "Неизвестно"

    if getattr(chat, "title", None):
        return chat.title

    full_name = f"{getattr(chat, 'first_name', '') or ''} {getattr(chat, 'last_name', '') or ''}".strip()

    if getattr(chat, "username", None):
        if full_name:
            return f"{full_name} (@{chat.username})"
        return f"@{chat.username}"

    return full_name or str(getattr(chat, "id", "Неизвестно"))


def cache_key(business_connection_id, chat_id, message_id):
    return f"{business_connection_id}:{chat_id}:{message_id}"


def detect_message_type(message: Message):
    if message.text:
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.video_note:
        return "video_note"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    if message.animation:
        return "animation"
    if message.sticker:
        return "sticker"
    return "other"


def clip_text(text: str, limit: int):
    if not text:
        return ""

    if len(text) <= limit:
        return text

    return text[:limit - 20] + "\n…[обрезано]"


def make_deleted_header(sender: str):
    return (
        "🗑 <b>СООБЩЕНИЕ УДАЛЕНО</b>\n"
        f"👤 <b>От:</b> {html.escape(sender)}\n"
        f"⏱ <b>Время удаления:</b> {now_time()}"
    )


def make_deleted_text_message(data: dict):
    text = data.get("text") or ""

    return (
        make_deleted_header(data["from"]) +
        "\n\n" +
        html.escape(clip_text(text, 3300))
    )


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton(text="🗑 Последние удаления", callback_data="admin:logs"),
            InlineKeyboardButton(text="💾 Все сообщения", callback_data="admin:all_logs"),
        ],
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close"),
        ],
    ])


def back_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin:menu")]
    ])


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить", callback_data="sub:check")],
    ])


def subscription_required_text():
    return (
        "🔒 <b>Доступ к боту ограничен</b>\n\n"
        "Для пользования данным ботом необходимо подписаться на канал автора.\n\n"
        f"📢 Канал: <b>{html.escape(REQUIRED_CHANNEL)}</b>\n\n"
        "После подписки нажми кнопку <b>✅ Проверить</b>."
    )


def greeting_text():
    return (
        "👋 <b>Привет! Я готов к работе.</b>\n\n"
        "Я буду сохранять удалённые сообщения, которые успею увидеть после подключения.\n\n"
        "<b>Как подключить меня:</b>\n"
        "1. Открой Telegram → Настройки.\n"
        "2. Перейди в Telegram Business / Автоматизация чатов.\n"
        "3. Выбери раздел Чат-боты.\n"
        "4. Добавь этого бота и включи доступ к нужным чатам.\n\n"
        "🔒 Никто посторонний тебя не читает: бот отправляет сохранённые удаления только в твой архивный чат."
    )


async def is_user_subscribed(user_id: int | None):
    if not user_id:
        return False

    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in {"creator", "administrator", "member"}
    except Exception as e:
        print(f"Не смог проверить подписку {user_id} на {REQUIRED_CHANNEL}: {e}")
        return False


async def send_subscription_gate(message: Message):
    await message.answer(
        subscription_required_text(),
        reply_markup=subscription_keyboard(),
        parse_mode="HTML"
    )


async def check_subscription_or_warn(message: Message):
    if await is_user_subscribed(message.from_user.id if message.from_user else None):
        return True

    await send_subscription_gate(message)
    return False


def logs_text(user_query: str | None = None, limit: int = 10):
    con = db_connect()
    cur = con.cursor()

    params = []

    if user_query:
        user_query = user_query.strip()

        if user_query.startswith("@"):
            username = user_query[1:]
            cur.execute("""
            SELECT id, source_user_id, source_name, source_username, chat_name, content_type, text, deleted_at
            FROM deleted_logs
            WHERE source_username = ?
            ORDER BY id DESC
            LIMIT ?
            """, (username, limit))
        elif user_query.lstrip("-").isdigit():
            cur.execute("""
            SELECT id, source_user_id, source_name, source_username, chat_name, content_type, text, deleted_at
            FROM deleted_logs
            WHERE source_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """, (int(user_query), limit))
        else:
            like = f"%{user_query}%"
            cur.execute("""
            SELECT id, source_user_id, source_name, source_username, chat_name, content_type, text, deleted_at
            FROM deleted_logs
            WHERE source_name LIKE ? OR source_username LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """, (like, like, limit))
    else:
        cur.execute("""
        SELECT id, source_user_id, source_name, source_username, chat_name, content_type, text, deleted_at
        FROM deleted_logs
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))

    rows = cur.fetchall()
    con.close()

    if not rows:
        if user_query:
            return f"🗑 Для запроса <code>{html.escape(user_query)}</code> удалённых сообщений пока нет."
        return "🗑 Удалённых сообщений пока нет."

    title = "🗑 <b>Последние удалённые сообщения</b>\n\n"
    if user_query:
        title = f"🗑 <b>Удалённые сообщения по запросу:</b> <code>{html.escape(user_query)}</code>\n\n"

    result = title

    for log_id, source_user_id, source_name, source_username, chat_name, content_type, text, deleted_at in rows:
        username_text = f"@{source_username}" if source_username else "без username"
        user_id_text = str(source_user_id) if source_user_id else "неизвестно"
        clean_text = html.escape(clip_text(text or "Без текста", 650))

        block = (
            f"#{log_id}\n"
            f"👤 <b>От:</b> {html.escape(source_name or 'Неизвестно')}\n"
            f"🆔 <b>ID:</b> <code>{html.escape(user_id_text)}</code>\n"
            f"🔗 <b>Username:</b> {html.escape(username_text)}\n"
            f"💬 <b>Чат:</b> {html.escape(chat_name or 'Неизвестно')}\n"
            f"📦 <b>Тип:</b> {html.escape(content_type or 'unknown')}\n"
            f"⏱ <b>Удалено:</b> {html.escape(deleted_at or '')}\n"
            f"📝 <b>Сообщение:</b>\n{clean_text}\n"
            "──────────────\n"
        )

        if len(result) + len(block) > 3900:
            result += "\n…часть записей скрыта из-за лимита Telegram."
            break

        result += block

    return result



def all_messages_text(user_query: str | None = None, limit: int = 10):
    con = db_connect()
    cur = con.cursor()

    if user_query:
        user_query = user_query.strip()

        if user_query.startswith("@"):
            username = user_query[1:]
            cur.execute("""
            SELECT id, event_type, source_user_id, source_name, source_username,
                   chat_name, content_type, text, media_file_path, media_filename, saved_at
            FROM all_message_logs
            WHERE source_username = ?
            ORDER BY id DESC
            LIMIT ?
            """, (username, limit))
        elif user_query.lstrip("-").isdigit():
            cur.execute("""
            SELECT id, event_type, source_user_id, source_name, source_username,
                   chat_name, content_type, text, media_file_path, media_filename, saved_at
            FROM all_message_logs
            WHERE source_user_id = ? OR chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """, (int(user_query), int(user_query), limit))
        else:
            like = f"%{user_query}%"
            cur.execute("""
            SELECT id, event_type, source_user_id, source_name, source_username,
                   chat_name, content_type, text, media_file_path, media_filename, saved_at
            FROM all_message_logs
            WHERE source_name LIKE ? OR source_username LIKE ? OR text LIKE ? OR chat_name LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """, (like, like, like, like, limit))
    else:
        cur.execute("""
        SELECT id, event_type, source_user_id, source_name, source_username,
               chat_name, content_type, text, media_file_path, media_filename, saved_at
        FROM all_message_logs
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))

    rows = cur.fetchall()
    con.close()

    if not rows:
        if user_query:
            return f"💾 Для запроса <code>{html.escape(user_query)}</code> сохранённых сообщений пока нет."
        return "💾 Сохранённых обычных сообщений пока нет."

    title = "💾 <b>Последние сохранённые сообщения</b>\n\n"
    if user_query:
        title = f"💾 <b>Сохранённые сообщения по запросу:</b> <code>{html.escape(user_query)}</code>\n\n"

    result = title

    for log_id, event_type, source_user_id, source_name, source_username, chat_name, content_type, text, media_file_path, media_filename, saved_at in rows:
        username_text = f"@{source_username}" if source_username else "без username"
        user_id_text = str(source_user_id) if source_user_id else "неизвестно"
        clean_text = html.escape(clip_text(text or "Без текста / медиа", 550))
        media_hint = ""

        if media_file_path:
            media_hint = f"\n📎 <b>Файл:</b> сохранён — <code>/all_send {log_id}</code>"
        elif media_filename:
            media_hint = f"\n📎 <b>Файл:</b> {html.escape(media_filename)}"

        block = (
            f"#{log_id}\n"
            f"👤 <b>От:</b> {html.escape(source_name or 'Неизвестно')}\n"
            f"🆔 <b>ID:</b> <code>{html.escape(user_id_text)}</code>\n"
            f"🔗 <b>Username:</b> {html.escape(username_text)}\n"
            f"💬 <b>Чат:</b> {html.escape(chat_name or 'Неизвестно')}\n"
            f"📦 <b>Тип:</b> {html.escape(content_type or 'unknown')} / {html.escape(event_type or 'message')}\n"
            f"⏱ <b>Сохранено:</b> {html.escape(saved_at or '')}"
            f"{media_hint}\n"
            f"📝 <b>Сообщение:</b>\n{clean_text}\n"
            "──────────────\n"
        )

        if len(result) + len(block) > 3900:
            result += "\n…часть записей скрыта из-за лимита Telegram."
            break

        result += block

    return result


def get_all_message_media(log_id: int):
    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    SELECT id, source_name, content_type, text, media_file_id, media_file_path, media_filename, saved_at
    FROM all_message_logs
    WHERE id = ?
    """, (log_id,))

    row = cur.fetchone()
    con.close()
    return row


def get_broadcast_targets():
    """
    Собирает всех получателей для рассылки.
    Важно: Telegram разрешает боту писать только тем, кто уже нажал /start.
    Поэтому здесь берём пользователей из базы + сохранённый archive_chat_id + ADMIN_IDS.
    """
    targets = set()

    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM users")
    for (user_id,) in cur.fetchall():
        if user_id:
            targets.add(int(user_id))
    con.close()

    try:
        archive_chat_id = get_archive_chat_id()
        if archive_chat_id:
            targets.add(int(archive_chat_id))
    except Exception:
        pass

    for admin_id in ADMIN_IDS:
        if admin_id:
            targets.add(int(admin_id))

    return sorted(targets)


async def download_to_memory(file_id: str):
    try:
        buffer = BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        print("Не удалось скачать файл в память:", e)
        return None


def sticker_filename(message: Message):
    if not message.sticker:
        return "deleted_sticker.webp"

    if message.sticker.is_animated:
        return "deleted_sticker.tgs"

    if message.sticker.is_video:
        return "deleted_sticker.webm"

    return "deleted_sticker.webp"



def media_info_from_message(message: Message):
    """
    Возвращает file_id, file_size, filename для медиа.
    Текстовые сообщения вернут None.
    """
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, getattr(photo, "file_size", None), "photo.jpg"

    if message.video:
        name = getattr(message.video, "file_name", None) or "video.mp4"
        return message.video.file_id, getattr(message.video, "file_size", None), name

    if message.video_note:
        return message.video_note.file_id, getattr(message.video_note, "file_size", None), "video_note.mp4"

    if message.voice:
        return message.voice.file_id, getattr(message.voice, "file_size", None), "voice.ogg"

    if message.audio:
        name = getattr(message.audio, "file_name", None) or "audio.mp3"
        return message.audio.file_id, getattr(message.audio, "file_size", None), name

    if message.document:
        name = getattr(message.document, "file_name", None) or "document.bin"
        return message.document.file_id, getattr(message.document, "file_size", None), name

    if message.animation:
        name = getattr(message.animation, "file_name", None) or "animation.mp4"
        return message.animation.file_id, getattr(message.animation, "file_size", None), name

    if message.sticker:
        return message.sticker.file_id, getattr(message.sticker, "file_size", None), sticker_filename(message)

    return None, None, None


def safe_media_filename(filename: str | None):
    filename = filename or "file.bin"
    bad = '<>:"/\\|?*\n\r\t'
    for ch in bad:
        filename = filename.replace(ch, "_")
    filename = filename.strip().strip(".") or "file.bin"
    return filename[:120]


async def save_media_to_disk(file_id: str | None, filename: str | None, file_size: int | None):
    if not file_id:
        return None

    if file_size and file_size > MAX_SAVE_MEDIA_MB * 1024 * 1024:
        print(f"Файл больше лимита архива: {file_size} байт")
        return None

    try:
        today_dir = ALL_MESSAGES_MEDIA_DIR / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)

        filename = safe_media_filename(filename)
        path = today_dir / f"{uuid4().hex}_{filename}"

        await bot.download(file_id, destination=path)
        return str(path)
    except Exception as e:
        print("Не удалось сохранить медиа в архив:", e)
        return None


async def save_all_message_log(message: Message, data: dict, event_type: str = "message"):
    """
    Сохраняет ЛЮБОЕ сообщение, которое бот увидел через Telegram Business:
    текст, подпись, тип, отправителя, чат и медиафайл на диск, если он не больше лимита.
    """
    if not SAVE_ALL_MESSAGES:
        return

    file_id, file_size, filename = media_info_from_message(message)
    media_path = await save_media_to_disk(file_id, filename, file_size)

    text = data.get("text") or data.get("caption") or ""
    if not text:
        text = "Без текста / медиафайл"

    if len(text) > 3500:
        text = text[:3500] + "\n…[обрезано]"

    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    INSERT INTO all_message_logs
    (business_connection_id, business_message_id, event_type,
     source_user_id, source_name, source_username, chat_id, chat_name,
     content_type, text, media_file_id, media_file_path, media_filename, saved_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        getattr(message, "business_connection_id", None),
        message.message_id,
        event_type,
        data.get("from_user_id"),
        data.get("from") or "Неизвестно",
        data.get("from_username"),
        data.get("chat_id"),
        data.get("chat_name") or "Неизвестно",
        data.get("type") or "unknown",
        text,
        file_id,
        media_path,
        filename,
        now_time(),
    ))

    con.commit()
    con.close()
    return media_path


async def remember_message(message: Message, event_type: str = "message"):
    business_connection_id = getattr(message, "business_connection_id", None)

    if not business_connection_id:
        return

    key = cache_key(
        business_connection_id,
        message.chat.id,
        message.message_id
    )

    msg_type = detect_message_type(message)
    source_user_id = message.from_user.id if message.from_user else None

    # Главное исправление:
    # если сообщение написал ты сам с основного Business-аккаунта,
    # бот запоминает только ключ как "игнорируемый" и не будет присылать его тебе при удалении.
    if should_ignore_sender(source_user_id):
        remember_ignored_own_message_key(key)
        print(f"Проигнорировал своё Business-сообщение: {key}, from_user_id={source_user_id}")
        return

    data = {
        "type": msg_type,

        "from_user_id": source_user_id,
        "from_username": message.from_user.username if message.from_user else None,
        "from": get_user_name(message.from_user),

        "chat_id": message.chat.id if message.chat else None,
        "chat_name": get_chat_name(message.chat),

        "text": message.text,
        "caption": message.caption,

        "photo_file_id": message.photo[-1].file_id if message.photo else None,
        "video_file_id": message.video.file_id if message.video else None,
        "video_note_file_id": message.video_note.file_id if message.video_note else None,
        "voice_file_id": message.voice.file_id if message.voice else None,
        "audio_file_id": message.audio.file_id if message.audio else None,
        "document_file_id": message.document.file_id if message.document else None,
        "animation_file_id": message.animation.file_id if message.animation else None,
        "sticker_file_id": message.sticker.file_id if message.sticker else None,

        "video_note_bytes": None,
        "voice_bytes": None,
        "sticker_bytes": None,
        "sticker_filename": None,
    }

    # Кружки скачиваем в память, чтобы потом можно было отправить как файл, если надо.
    if message.video_note:
        data["video_note_bytes"] = await download_to_memory(message.video_note.file_id)

    # ГС скачиваем в память и потом отправляем как документ,
    # чтобы не было ошибки VOICE_MESSAGES_FORBIDDEN.
    if message.voice:
        data["voice_bytes"] = await download_to_memory(message.voice.file_id)

    # Стикеры тоже лучше держать как файл.
    if message.sticker:
        data["sticker_bytes"] = await download_to_memory(message.sticker.file_id)
        data["sticker_filename"] = sticker_filename(message)

    # Сохраняем доступные данные в SQLite и медиа на диск (если включён архив).
    data["media_file_path"] = await save_all_message_log(message, data, event_type=event_type)

    message_cache[key] = data
    message_cache.move_to_end(key)

    while len(message_cache) > MAX_CACHE_SIZE:
        message_cache.popitem(last=False)

    print(f"Запомнил сообщение: {key}, type={msg_type}")

    # Сразу отправляем доступную копию в личный архив. Ошибка отправки
    # не должна мешать сохранению сообщения в кэш/базу.
    await send_saved_copy_to_archive(message, data)


async def send_media_separately(archive_chat_id: int, data: dict):
    msg_type = data["type"]

    # 1. Сначала отдельное сообщение-заголовок.
    await bot.send_message(
        chat_id=archive_chat_id,
        text=make_deleted_header(data["from"]),
        parse_mode="HTML"
    )

    # 2. Потом отдельно само содержимое.
    if msg_type == "photo":
        await bot.send_photo(
            chat_id=archive_chat_id,
            photo=data["photo_file_id"]
        )

    elif msg_type == "video":
        await bot.send_video(
            chat_id=archive_chat_id,
            video=data["video_file_id"]
        )

    elif msg_type == "video_note":
        try:
            await bot.send_video_note(
                chat_id=archive_chat_id,
                video_note=data["video_note_file_id"]
            )
        except Exception:
            if data["video_note_bytes"]:
                file = BufferedInputFile(
                    data["video_note_bytes"],
                    filename="deleted_video_note.mp4"
                )
                await bot.send_document(
                    chat_id=archive_chat_id,
                    document=file
                )
            else:
                await bot.send_message(
                    chat_id=archive_chat_id,
                    text="⚠️ Кружок был удалён, но файл не успел сохраниться."
                )

    elif msg_type == "voice":
        # ВАЖНО: отправляем ГС как файл, а не как voice.
        # Так обходим ошибку VOICE_MESSAGES_FORBIDDEN.
        if data["voice_bytes"]:
            file = BufferedInputFile(
                data["voice_bytes"],
                filename="deleted_voice.ogg"
            )
            await bot.send_document(
                chat_id=archive_chat_id,
                document=file
            )
        elif data.get("voice_file_id"):
            try:
                await bot.send_document(
                    chat_id=archive_chat_id,
                    document=data["voice_file_id"],
                    caption="Сохранённое голосовое сообщение"
                )
            except TelegramAPIError:
                await bot.send_message(
                    chat_id=archive_chat_id,
                    text="⚠️ Голосовое было удалено, но Telegram не дал повторно отправить файл."
                )
        else:
            await bot.send_message(
                chat_id=archive_chat_id,
                text="⚠️ Голосовое было удалено, но доступная копия не найдена."
            )

    elif msg_type == "audio":
        await bot.send_audio(
            chat_id=archive_chat_id,
            audio=data["audio_file_id"]
        )

    elif msg_type == "document":
        await bot.send_document(
            chat_id=archive_chat_id,
            document=data["document_file_id"]
        )

    elif msg_type == "animation":
        await bot.send_animation(
            chat_id=archive_chat_id,
            animation=data["animation_file_id"]
        )

    elif msg_type == "sticker":
        if data["sticker_bytes"]:
            file = BufferedInputFile(
                data["sticker_bytes"],
                filename=data["sticker_filename"] or "deleted_sticker.webp"
            )
            await bot.send_document(
                chat_id=archive_chat_id,
                document=file
            )
        else:
            await bot.send_sticker(
                chat_id=archive_chat_id,
                sticker=data["sticker_file_id"]
            )

    else:
        await bot.send_message(
            chat_id=archive_chat_id,
            text="⚠️ Тип удалённого сообщения пока не поддерживается."
        )


def get_saved_business_message(business_connection_id: str, chat_id: int, message_id: int):
    """Восстанавливает последнее сохранённое сообщение из SQLite после перезапуска."""
    if not SAVE_ALL_MESSAGES:
        return None

    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        SELECT source_user_id, source_name, source_username, chat_id, chat_name,
               content_type, text, media_file_id, media_file_path, media_filename
        FROM all_message_logs
        WHERE business_connection_id = ? AND chat_id = ? AND business_message_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (business_connection_id, chat_id, message_id))
    row = cur.fetchone()
    con.close()

    if not row:
        return None

    (source_user_id, source_name, source_username, saved_chat_id, chat_name,
     content_type, saved_text, media_file_id, media_file_path, media_filename) = row

    data = {
        "type": content_type or "other",
        "from_user_id": source_user_id,
        "from_username": source_username,
        "from": source_name or "Неизвестно",
        "chat_id": saved_chat_id,
        "chat_name": chat_name or "Неизвестно",
        "text": saved_text if content_type == "text" else None,
        "caption": saved_text if content_type != "text" else None,
        "photo_file_id": media_file_id if content_type == "photo" else None,
        "video_file_id": media_file_id if content_type == "video" else None,
        "video_note_file_id": media_file_id if content_type == "video_note" else None,
        "voice_file_id": media_file_id if content_type == "voice" else None,
        "audio_file_id": media_file_id if content_type == "audio" else None,
        "document_file_id": media_file_id if content_type == "document" else None,
        "animation_file_id": media_file_id if content_type == "animation" else None,
        "sticker_file_id": media_file_id if content_type == "sticker" else None,
        "video_note_bytes": None,
        "voice_bytes": None,
        "sticker_bytes": None,
        "sticker_filename": media_filename,
        "media_file_path": media_file_path,
    }

    # Для специальных типов используем локальную копию, если она сохранилась.
    if media_file_path and Path(media_file_path).is_file():
        try:
            raw = Path(media_file_path).read_bytes()
            if content_type == "video_note":
                data["video_note_bytes"] = raw
            elif content_type == "voice":
                data["voice_bytes"] = raw
            elif content_type == "sticker":
                data["sticker_bytes"] = raw
        except OSError as e:
            print("Не удалось прочитать локальную копию медиа:", e)

    return data


async def send_saved_copy_to_archive(message: Message, data: dict):
    """Отправляет в личный архив копию того, что Telegram передал боту."""
    archive_chat_id = get_archive_chat_id()
    if not archive_chat_id:
        return

    # Сохраняем прежнее правило подписки для владельца архивного чата.
    if not await is_user_subscribed(archive_chat_id):
        print("Копия не отправлена: архивный чат не подписан на обязательный канал.")
        return

    sender = html.escape(data.get("from") or "Неизвестно")
    msg_type = html.escape(data.get("type") or "other")
    header = (
        "💾 <b>СООБЩЕНИЕ СОХРАНЕНО</b>\n"
        f"👤 <b>От:</b> {sender}\n"
        f"📦 <b>Тип:</b> {msg_type}\n"
        f"⏱ <b>Время:</b> {now_time()}"
    )
    body = message.text or message.caption or ""

    try:
        if message.text:
            text = clip_text(body, 2800)
            await bot.send_message(
                chat_id=archive_chat_id,
                text=header + ("\n\n" + html.escape(text) if text else ""),
                parse_mode="HTML"
            )
            return

        # Для медиа сначала отправляем уведомление, затем сам доступный файл.
        await bot.send_message(chat_id=archive_chat_id, text=header, parse_mode="HTML")
        caption = clip_text(body, 900) or None

        if message.photo:
            await bot.send_photo(chat_id=archive_chat_id, photo=message.photo[-1].file_id, caption=caption)
        elif message.video:
            await bot.send_video(chat_id=archive_chat_id, video=message.video.file_id, caption=caption)
        elif message.audio:
            await bot.send_audio(chat_id=archive_chat_id, audio=message.audio.file_id, caption=caption)
        elif message.document:
            await bot.send_document(chat_id=archive_chat_id, document=message.document.file_id, caption=caption)
        elif message.animation:
            await bot.send_animation(chat_id=archive_chat_id, animation=message.animation.file_id, caption=caption)
        elif message.video_note:
            try:
                await bot.send_video_note(chat_id=archive_chat_id, video_note=message.video_note.file_id)
            except TelegramAPIError:
                raw = data.get("video_note_bytes")
                if raw:
                    await bot.send_document(chat_id=archive_chat_id, document=BufferedInputFile(raw, filename="video_note.mp4"))
                else:
                    await bot.send_message(chat_id=archive_chat_id, text="⚠️ Telegram не предоставил повторно отправляемую копию кружка.")
        elif message.voice:
            raw = data.get("voice_bytes")
            if raw:
                await bot.send_document(
                    chat_id=archive_chat_id,
                    document=BufferedInputFile(raw, filename="voice.ogg"),
                    caption=caption
                )
            else:
                try:
                    await bot.send_document(chat_id=archive_chat_id, document=message.voice.file_id, caption=caption)
                except TelegramAPIError:
                    await bot.send_message(chat_id=archive_chat_id, text="⚠️ Telegram не предоставил повторно отправляемую копию голосового сообщения.")
        elif message.sticker:
            raw = data.get("sticker_bytes")
            if raw:
                await bot.send_document(
                    chat_id=archive_chat_id,
                    document=BufferedInputFile(raw, filename=data.get("sticker_filename") or "sticker.webp")
                )
            else:
                await bot.send_sticker(chat_id=archive_chat_id, sticker=message.sticker.file_id)
        else:
            await bot.send_message(
                chat_id=archive_chat_id,
                text="ℹ️ Сообщение получено, но копирование этого типа содержимого не поддерживается текущим обработчиком."
            )
    except TelegramAPIError as e:
        print(f"Не удалось отправить сохранённую копию в архив: {e}")
    except Exception as e:
        print(f"Неожиданная ошибка при отправке копии в архив: {e}")


async def send_deleted_message(archive_chat_id: int, data: dict):
    msg_type = data["type"]

    if msg_type == "text":
        await bot.send_message(
            chat_id=archive_chat_id,
            text=make_deleted_text_message(data),
            parse_mode="HTML"
        )
    else:
        await send_media_separately(archive_chat_id, data)


@dp.message(Command("start"))
async def start_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if not await check_subscription_or_warn(message):
        return

    save_archive_chat_id(message.chat.id)

    await message.answer(
        greeting_text(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "sub:check")
async def check_subscription_callback(callback: CallbackQuery):
    if await is_user_subscribed(callback.from_user.id):
        remember_bot_user(callback.from_user)
        save_archive_chat_id(callback.message.chat.id)

        await callback.message.edit_text(
            greeting_text(),
            parse_mode="HTML"
        )
        await callback.answer("Подписка подтверждена ✅")
    else:
        await callback.answer(
            "Подписка не найдена. Подпишись на канал и нажми проверку ещё раз.",
            show_alert=True
        )


@dp.message(Command("id"))
async def id_handler(message: Message):
    if message.chat.type == "private":
        remember_bot_user(message.from_user)

        if not await check_subscription_or_warn(message):
            return

    await message.answer(
        f"Твой chat_id:\n<code>{message.chat.id}</code>\n\n"
        f"Твой user_id:\n<code>{message.from_user.id}</code>",
        parse_mode="HTML"
    )


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.from_user.id not in ADMIN_IDS and not await check_subscription_or_warn(message):
        return

    parts = message.text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""

    if message.from_user.id in ADMIN_IDS or code == ADMIN_CODE:
        ADMIN_SESSIONS.add(message.from_user.id)

        await message.answer(
            "🔐 <b>Админ-панель открыта.</b>\n\n"
            "Выбери действие:",
            reply_markup=admin_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer("⛔ Нет доступа.")


@dp.message(Command("logs"))
async def logs_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.from_user.id not in ADMIN_IDS and not await check_subscription_or_warn(message):
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None

    await message.answer(
        logs_text(query),
        parse_mode="HTML"
    )



@dp.message(Command("all_logs"))
async def all_logs_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.from_user.id not in ADMIN_IDS and not await check_subscription_or_warn(message):
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None

    await message.answer(
        all_messages_text(query),
        parse_mode="HTML"
    )


@dp.message(Command("all_send"))
async def all_send_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.from_user.id not in ADMIN_IDS and not await check_subscription_or_warn(message):
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Напиши так: <code>/all_send ID_записи</code>", parse_mode="HTML")
        return

    log_id = int(parts[1].strip())
    row = get_all_message_media(log_id)

    if not row:
        await message.answer("Запись не найдена.")
        return

    _, source_name, content_type, text, media_file_id, media_file_path, media_filename, saved_at = row

    caption = (
        f"💾 <b>Сохранённое сообщение #{log_id}</b>\n"
        f"👤 <b>От:</b> {html.escape(source_name or 'Неизвестно')}\n"
        f"📦 <b>Тип:</b> {html.escape(content_type or 'unknown')}\n"
        f"⏱ <b>Сохранено:</b> {html.escape(saved_at or '')}\n\n"
        f"{html.escape(clip_text(text or 'Без текста', 800))}"
    )

    if media_file_path and Path(media_file_path).exists():
        try:
            await bot.send_document(
                chat_id=message.chat.id,
                document=BufferedInputFile(Path(media_file_path).read_bytes(), filename=media_filename or Path(media_file_path).name),
                caption=caption,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            await message.answer(f"Не смог отправить файл с диска: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
            return

    if media_file_id:
        try:
            await bot.send_document(
                chat_id=message.chat.id,
                document=media_file_id,
                caption=caption,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            await message.answer(f"Не смог отправить файл по file_id: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
            return

    await message.answer(caption, parse_mode="HTML")


@dp.message(Command("broadcast"))
async def broadcast_command_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.from_user.id not in ADMIN_IDS and not await check_subscription_or_warn(message):
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    ADMIN_STATE[message.from_user.id] = "broadcast"

    await message.answer(
        "📢 <b>Broadcast</b>\n\n"
        "Напиши следующим сообщением текст рассылки.\n\n"
        "Отмена: /cancel",
        reply_markup=back_admin_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    if message.from_user:
        ADMIN_STATE.pop(message.from_user.id, None)

    await message.answer("Ок, действие отменено.")


@dp.callback_query(F.data == "admin:menu")
async def admin_menu_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "🔐 <b>Админ-панель открыта.</b>\n\nВыбери действие:",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:close")
async def admin_close_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.edit_text("Админ-панель закрыта.")
    await callback.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    con = db_connect()
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM deleted_logs")
    deleted_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT source_user_id) FROM deleted_logs WHERE source_user_id IS NOT NULL")
    deleted_users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM deleted_logs WHERE deleted_at LIKE ?", (datetime.now().strftime("%d.%m.%Y") + "%",))
    today_deleted_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM all_message_logs")
    all_messages_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM all_message_logs WHERE saved_at LIKE ?", (datetime.now().strftime("%d.%m.%Y") + "%",))
    today_all_messages_count = cur.fetchone()[0]

    con.close()

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей в базе: <b>{users_count}</b>\n"
        f"💾 Всего обычных сообщений: <b>{all_messages_count}</b>\n"
        f"📅 Обычных сообщений сегодня: <b>{today_all_messages_count}</b>\n"
        f"🗑 Всего сохранённых удалений: <b>{deleted_count}</b>\n"
        f"👤 Аккаунтов с удалениями: <b>{deleted_users_count}</b>\n"
        f"📅 Удалений сегодня: <b>{today_deleted_count}</b>\n"
        f"🕒 Сейчас: <code>{html.escape(now_time())}</code>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:users")
async def admin_users_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    SELECT user_id, full_name, username, first_seen, last_seen
    FROM users
    ORDER BY last_seen DESC
    LIMIT 20
    """)

    rows = cur.fetchall()
    con.close()

    if not rows:
        text = "👥 Пользователей пока нет."
    else:
        text = "👥 <b>Последние пользователи бота</b>\n\n"

        for user_id, full_name, username, first_seen, last_seen in rows:
            username_text = f"@{username}" if username else "без username"
            block = (
                f"👤 {html.escape(full_name or 'Неизвестно')}\n"
                f"🆔 <code>{user_id}</code>\n"
                f"🔗 {html.escape(username_text)}\n"
                f"🕒 Последний раз: {html.escape(last_seen or '')}\n"
                "──────────────\n"
            )

            if len(text) + len(block) > 3900:
                text += "\n…часть пользователей скрыта из-за лимита Telegram."
                break

            text += block

        text += "\nЧтобы посмотреть удаления конкретного аккаунта:\n<code>/logs USER_ID</code>\nили\n<code>/logs @username</code>"

    await callback.message.edit_text(
        text,
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:logs")
async def admin_logs_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        logs_text(),
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()



@dp.callback_query(F.data == "admin:all_logs")
async def admin_all_logs_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        all_messages_text(),
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    ADMIN_STATE[callback.from_user.id] = "broadcast"

    await callback.message.edit_text(
        "📢 <b>Broadcast</b>\n\n"
        "Напиши следующим сообщением текст рассылки.\n\n"
        "Отмена: /cancel",
        reply_markup=back_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(
    lambda message: message.from_user
    and is_admin(message.from_user.id)
    and ADMIN_STATE.get(message.from_user.id) == "broadcast"
)
async def broadcast_input_handler(message: Message):
    if message.chat.type != "private":
        return

    remember_bot_user(message.from_user)

    if message.text == "/cancel":
        ADMIN_STATE.pop(message.from_user.id, None)
        await message.answer(
            "Broadcast отменён.",
            reply_markup=admin_keyboard()
        )
        return

    text_to_send = message.text or message.caption

    if not text_to_send:
        await message.answer("Нужно отправить именно текст.")
        return

    ADMIN_STATE.pop(message.from_user.id, None)

    users = get_broadcast_targets()

    if not users:
        await message.answer(
            "⚠️ Получателей пока нет. С нужного аккаунта открой бота и нажми /start.",
            reply_markup=admin_keyboard()
        )
        return

    success = 0
    failed = 0
    failed_ids = []

    await message.answer(f"📢 Начинаю рассылку. Получателей: {len(users)}")

    for user_id in users:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"📢 <b>Сообщение от администратора:</b>\n\n{html.escape(text_to_send)}",
                parse_mode="HTML"
            )
            success += 1
            await asyncio.sleep(0.05)

        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"📢 <b>Сообщение от администратора:</b>\n\n{html.escape(text_to_send)}",
                    parse_mode="HTML"
                )
                success += 1
            except TelegramAPIError:
                failed += 1
                failed_ids.append(user_id)

        except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError):
            failed += 1
            failed_ids.append(user_id)

    failed_hint = ""
    if failed_ids:
        failed_hint = "\n\nНе дошло до ID: " + ", ".join(str(x) for x in failed_ids[:10])
        if len(failed_ids) > 10:
            failed_hint += " ..."

    await message.answer(
        "✅ <b>Broadcast завершён.</b>\n\n"
        f"Получателей: <b>{len(users)}</b>\n"
        f"Успешно: <b>{success}</b>\n"
        f"Ошибок: <b>{failed}</b>"
        f"{html.escape(failed_hint)}",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


@dp.message()
async def private_fallback_handler(message: Message):
    """
    Сохраняем пользователя, если он просто написал боту в личку.
    Это нужно, чтобы статистика и broadcast видели активных пользователей.
    """
    if message.chat.type == "private":
        remember_bot_user(message.from_user)

        if not await is_user_subscribed(message.from_user.id if message.from_user else None):
            await send_subscription_gate(message)


@dp.business_connection()
async def business_connection_handler(connection: BusinessConnection):
    # Уведомления вида "Business-бот подключён/отключён" в Telegram больше не отправляем.
    # Оставляем только вывод в консоль, чтобы можно было проверить подключение на сервере.
    print(f"Business connection: {connection.id}, enabled={connection.is_enabled}")


@dp.business_message()
async def business_message_handler(message: Message):
    await remember_message(message, event_type="message")


@dp.edited_business_message()
async def edited_business_message_handler(message: Message):
    await remember_message(message, event_type="edited")


@dp.deleted_business_messages()
async def deleted_business_messages_handler(event: BusinessMessagesDeleted):
    archive_chat_id = get_archive_chat_id()

    if not archive_chat_id:
        print("Сначала напиши боту /start в личку.")
        return

    if not await is_user_subscribed(archive_chat_id):
        try:
            await bot.send_message(
                chat_id=archive_chat_id,
                text=subscription_required_text(),
                reply_markup=subscription_keyboard(),
                parse_mode="HTML"
            )
        except Exception as e:
            print("Не смог отправить сообщение о подписке в архивный чат:", e)
        return

    for message_id in event.message_ids:
        key = cache_key(
            event.business_connection_id,
            event.chat.id,
            message_id
        )

        if was_ignored_own_message_key(key):
            forget_ignored_own_message_key(key)
            print(f"Удаление своего сообщения проигнорировано: {key}")
            continue

        data = message_cache.get(key)
        if not data:
            data = get_saved_business_message(
                event.business_connection_id,
                event.chat.id,
                message_id
            )
            if data:
                print(f"Восстановил удалённое сообщение из SQLite: {key}")

        # На случай, если своё сообщение уже попало в старый cache до перезапуска/обновления кода.
        if data and should_ignore_sender(data.get("from_user_id")):
            message_cache.pop(key, None)
            forget_ignored_own_message_key(key)
            print(f"Удаление своего сообщения из cache проигнорировано: {key}")
            continue

        if not data:
            save_deleted_log(None, event.chat)

            await bot.send_message(
                chat_id=archive_chat_id,
                text=(
                    "🗑 <b>СООБЩЕНИЕ УДАЛЕНО</b>\n"
                    "👤 <b>От:</b> неизвестно\n"
                    f"⏱ <b>Время удаления:</b> {now_time()}\n\n"
                    "⚠️ Я не успел запомнить это сообщение или бот был перезапущен."
                ),
                parse_mode="HTML"
            )
            continue

        # Сохраняем удаление в базу для админ-панели.
        save_deleted_log(data, event.chat)

        try:
            await send_deleted_message(archive_chat_id, data)
        except Exception as e:
            await bot.send_message(
                chat_id=archive_chat_id,
                text=(
                    make_deleted_header(data["from"]) +
                    "\n\n⚠️ Не смог отправить удалённое сообщение.\n"
                    f"Ошибка: {html.escape(str(e))}"
                ),
                parse_mode="HTML"
            )

        message_cache.pop(key, None)

        print(f"Отправил удалённое сообщение: {key}")


async def main():
    init_db()

    print("Бот запущен.")
    print("Текстовые удалённые сообщения — одним сообщением.")
    print("Фото/видео/кружки/гс — отдельно.")
    print("Архив любых сообщений включён:", SAVE_ALL_MESSAGES)
    print("Доступные копии отправляются в личный архивный чат.")
    print("Папка медиа-архива:", ALL_MESSAGES_MEDIA_DIR.resolve())
    print("Сначала напиши боту /start в личку.")

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages"
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())
