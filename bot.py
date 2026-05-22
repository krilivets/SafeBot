import asyncio
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv
import yt_dlp


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в .env")

# Telegram обычно стабильно принимает файлы до ~50 МБ через Bot API.
# Поэтому по умолчанию ставим 45 МБ, чтобы был запас.
MAX_MB = int(os.getenv("MAX_MB", "45"))
MAX_BYTES = MAX_MB * 1024 * 1024

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Чтобы бот не умер от нескольких тяжёлых скачиваний одновременно.
download_semaphore = asyncio.Semaphore(2)

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "instagr.am",
)

# Ищем ссылки и с https://, и без протокола: youtube.com/..., www.instagram.com/...
URL_RE = re.compile(
    r"(?:(?:https?://)?(?:www\.|m\.)?(?:"
    r"youtube\.com|youtu\.be|"
    r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|"
    r"instagram\.com|instagr\.am"
    r")/[^\s<>]+)",
    re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    url = (url or "").strip()

    # Убираем лишние символы, которые часто прилипают при копировании.
    url = url.rstrip(".,)]}>\"'»")
    url = url.lstrip("(<[{\"'«")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url


def clean_host(host: str) -> str:
    host = host.lower().strip()

    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    return host


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = clean_host(parsed.netloc)
        return any(host == domain or host.endswith("." + domain) for domain in SUPPORTED_DOMAINS)
    except Exception:
        return False


def extract_supported_url(text: str) -> str | None:
    if not text:
        return None

    for match in URL_RE.finditer(text):
        url = normalize_url(match.group(0))
        if is_supported_url(url):
            return url

    return None


def human_size(size_bytes: int) -> str:
    mb = size_bytes / 1024 / 1024
    return f"{mb:.1f} МБ"


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def get_source_name(url: str) -> str:
    host = clean_host(urlparse(url).netloc)

    if "youtu" in host or "youtube" in host:
        return "YouTube"
    if "instagram" in host or "instagr.am" in host:
        return "Instagram"
    if "tiktok" in host:
        return "TikTok"

    return host


def start_text() -> str:
    return (
        "👋 Привет! Я готов к работе.\n\n"
        "Просто отправь мне ссылку на видео — я попробую скачать его и прислать файлом.\n\n"
        "✅ Принимаю ссылки из:\n"
        "• YouTube / YouTube Shorts\n"
        "• Instagram Reels / видео-посты\n"
        "• TikTok\n\n"
        "⚠️ Скачиваются только открытые видео. Приватные аккаунты, платный контент "
        "и ролики без доступа бот не скачивает."
    )


def download_video_sync(url: str, task_dir: Path) -> dict:
    """
    Скачивает публичное видео через yt-dlp.
    Не обходит приватные аккаунты, платный доступ, DRM и закрытые ролики.
    """

    outtmpl = str(task_dir / "%(title).80s_%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 30,
        "retries": 3,
        # Берём файл до 720p и стараемся не выйти за лимит Telegram.
        "format": (
            "best[ext=mp4][height<=720][filesize<{}]/"
            "best[ext=mp4][height<=720][filesize_approx<{}]/"
            "best[height<=720][filesize<{}]/"
            "best[height<=720][filesize_approx<{}]/"
            "best[ext=mp4][height<=480]/"
            "best[height<=480]/"
            "best"
        ).format(MAX_BYTES, MAX_BYTES, MAX_BYTES, MAX_BYTES),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("Не удалось получить информацию о видео.")

        expected_path = Path(ydl.prepare_filename(info))

    files = [p for p in task_dir.iterdir() if p.is_file()]

    if expected_path.exists():
        file_path = expected_path
    elif files:
        file_path = max(files, key=lambda p: p.stat().st_size)
    else:
        raise RuntimeError("Видео не скачалось или файл не найден.")

    size = file_path.stat().st_size

    if size > MAX_BYTES:
        raise RuntimeError(
            f"Видео получилось слишком большим: {human_size(size)}. "
            f"Лимит сейчас: до {MAX_MB} МБ."
        )

    return {
        "file_path": file_path,
        "title": info.get("title") or "Видео",
        "source": get_source_name(url),
        "webpage_url": info.get("webpage_url") or url,
        "size": size,
        "task_dir": task_dir,
    }


async def download_video(url: str) -> dict:
    task_dir = DOWNLOAD_DIR / str(uuid4())
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, download_video_sync, url, task_dir)
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(start_text())


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(start_text())


@dp.message(F.text | F.caption)
async def link_handler(message: Message):
    text = message.text or message.caption or ""
    url = extract_supported_url(text)

    if not url:
        await message.answer(
            "🔗 Пришли ссылку на видео из YouTube, Instagram или TikTok.\n\n"
            "Можно так:\n"
            "• https://youtube.com/shorts/...\n"
            "• https://youtu.be/...\n"
            "• https://instagram.com/reel/...\n"
            "• https://tiktok.com/@user/video/..."
        )
        return

    source = get_source_name(url)
    status = await message.answer(f"⏳ Нашёл ссылку {source}. Скачиваю видео...")

    result = None

    async with download_semaphore:
        try:
            result = await download_video(url)

            file_path: Path = result["file_path"]
            size_text = human_size(result["size"])
            title = clip(result["title"], 80)
            source = result["source"]

            caption = clip(
                f"✅ Готово!\n\n"
                f"🎬 {title}\n"
                f"🌐 Источник: {source}\n"
                f"📦 Размер: {size_text}",
                1000,
            )

            try:
                await message.answer_video(
                    video=FSInputFile(file_path),
                    caption=caption,
                    supports_streaming=True,
                )
            except Exception:
                await message.answer_document(
                    document=FSInputFile(file_path),
                    caption=caption,
                )

            await status.delete()

        except yt_dlp.utils.DownloadError as e:
            await status.edit_text(
                "❌ Не получилось скачать видео.\n\n"
                "Возможные причины:\n"
                "• видео приватное или закрытое;\n"
                "• сайт требует вход в аккаунт;\n"
                "• ролик слишком большой;\n"
                "• платформа временно не отдаёт видео.\n\n"
                f"Технически: {clip(str(e), 500)}"
            )

        except Exception as e:
            await status.edit_text(
                "❌ Ошибка при скачивании.\n\n"
                f"{clip(str(e), 700)}"
            )

        finally:
            if result and result.get("task_dir"):
                shutil.rmtree(result["task_dir"], ignore_errors=True)


async def main():
    print("Video Saver Bot запущен.")
    print("Принимаю ссылки: YouTube, Instagram, TikTok.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
