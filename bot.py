import os
import asyncio
import glob
import re
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# ПРАВИЛЬНЫЙ ИМПОРТ GEMINI
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_urls = {}
user_locks = {}

# ИНИЦИАЛИЗАЦИЯ
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        # Используем проверенный метод
        model = genai.GenerativeModel('gemini-1.5-flash')
        logging.info("✅ Gemini AI подключен")
    except Exception as e:
        logging.error(f"❌ Ошибка ИИ: {e}")
        model = None
else:
    model = None

PINTEREST_RE = r'(https?://(?:www\.)?(?:pinterest\.com/pin/|pin\.it)/?\S+)'

# --- ФУНКЦИИ ОБРАБОТКИ ---

async def safe_edit(message: types.Message, text: str):
    try: await message.edit_text(text, parse_mode="Markdown")
    except: pass

async def process_video(input_path, output_path):
    """Исправляет звук и картинку для телефонов"""
    cmd = [
        "ffmpeg", "-i", input_path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-y", output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    return os.path.exists(output_path)

async def download_media(url, uid, mode="video"):
    raw_path = f"{DOWNLOAD_DIR}/{uid}_raw.%(ext)s"
    ydl_opts = [
        "yt-dlp", "-o", raw_path, "--no-playlist", "--no-warnings",
        "-f", "bestvideo+bestaudio/best" if mode == "video" else "bestaudio/best"
    ]
    if mode == "audio":
        ydl_opts += ["-x", "--audio-format", "mp3"]
    
    ydl_opts.append(url)
    proc = await asyncio.create_subprocess_exec(*ydl_opts)
    await proc.wait()
    
    files = glob.glob(f"{DOWNLOAD_DIR}/{uid}_raw.*")
    if not files: return None
    
    infile = files[0]
    if mode == "audio" or infile.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return infile
    
    outfile = f"{DOWNLOAD_DIR}/{uid}_ready.mp4"
    if await process_video(infile, outfile):
        if os.path.exists(infile): os.remove(infile)
        return outfile
    return infile

# --- ОБРАБОТЧИКИ ---

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer("🚀 Бот запущен! Присылай ссылки.")

@dp.message_handler(regexp=PINTEREST_RE)
async def handle_pinterest(message: types.Message):
    url = re.search(PINTEREST_RE, message.text).group(1)
    status = await message.answer("🖼 Pinterest...")
    fpath = await download_media(url, message.from_user.id)
    if fpath:
        with open(fpath, 'rb') as f:
            if any(fpath.lower().endswith(x) for x in ['.jpg', '.png', '.jpeg', '.webp']):
                await bot.send_photo(message.from_user.id, f)
            else:
                await bot.send_video(message.from_user.id, f)
        await status.delete()
        os.remove(fpath)
    else:
        await safe_edit(status, "❌ Ошибка скачивания.")

@dp.message_handler(lambda m: "http" in m.text)
async def handle_urls(message: types.Message):
    if re.search(PINTEREST_RE, message.text): return
    url = re.search(r'(https?://\S+)', message.text).group(1)
    user_urls[message.from_user.id] = url
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🎬 Видео", callback_data="get_video"),
        InlineKeyboardButton("🎵 Аудио", callback_data="get_audio")
    )
    await message.answer("Что скачать?", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('get_'))
async def query_handler(callback: CallbackQuery):
    uid, mode = callback.from_user.id, callback.data.split('_')[1]
    url = user_urls.get(uid)
    status = await callback.message.edit_text("⏳ Качаю...")
    fpath = await download_media(url, uid, mode)
    if fpath:
        with open(fpath, 'rb') as f:
            if mode == "audio": await bot.send_audio(uid, f)
            else: await bot.send_video(uid, f)
        await status.delete()
        os.remove(fpath)
    else:
        await safe_edit(status, "❌ Ошибка.")

@dp.message_handler(commands=['ask'])
async def ask(message: types.Message):
    q = message.get_args()
    if not q or not model: return
    status = await message.answer("🤔")
    try:
        response = await asyncio.to_thread(model.generate_content, q)
        await status.edit_text(response.text)
    except Exception as e:
        await status.edit_text(f"Ошибка: {e}")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
