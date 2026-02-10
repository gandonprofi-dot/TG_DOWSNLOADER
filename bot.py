import os
import asyncio
import glob
import re
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import google.generativeai as genai

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_urls = {}
user_locks = {}

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')

PINTEREST_RE = r'(https?://(?:www\.)?(?:pinterest\.com/pin/|pin\.it)/?\S+)'

async def safe_edit(message: types.Message, text: str):
    try: await message.edit_text(text, parse_mode="Markdown")
    except: pass

# --- МОЩНАЯ ФУНКЦИЯ ОБРАБОТКИ ВИДЕО ---
async def process_video(input_path, output_path):
    """Конвертация видео в формат, который идеально читается на телефонах"""
    cmd = [
        "ffmpeg", "-i", input_path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-y", output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    return os.path.exists(output_path)

async def download_media(url, uid, mode="video"):
    raw_path = f"{DOWNLOAD_DIR}/{uid}_raw.%(ext)s"
    
    # Параметры скачивания
    ydl_opts = [
        "yt-dlp", "-o", raw_path, "--no-playlist", "--no-warnings",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ]
    
    if mode == "audio":
        ydl_opts += ["-x", "--audio-format", "mp3"]
    else:
        # Берем лучшее видео со звуком
        ydl_opts += ["-f", "bestvideo+bestaudio/best"]

    ydl_opts.append(url)
    
    proc = await asyncio.create_subprocess_exec(*ydl_opts, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    
    downloaded_files = glob.glob(f"{DOWNLOAD_DIR}/{uid}_raw.*")
    if not downloaded_files:
        return None
    
    input_file = downloaded_files[0]
    
    if mode == "audio" or input_file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return input_file
    
    # Конвертируем видео для совместимости с телефонами
    final_video = f"{DOWNLOAD_DIR}/{uid}_final.mp4"
    success = await process_video(input_file, final_video)
    
    if success:
        if os.path.exists(input_file): os.remove(input_file)
        return final_video
    return input_file

# --- ОБРАБОТЧИКИ ---

@dp.message_handler(regexp=PINTEREST_RE)
async def handle_pinterest(message: types.Message):
    url = re.search(PINTEREST_RE, message.text).group(1)
    uid = message.from_user.id
    status = await message.answer("🖼 Pinterest... Качаю медиа...")
    
    try:
        fpath = await download_media(url, uid)
        if not fpath: raise Exception("Pinterest не отдал файл")
        
        ext = os.path.splitext(fpath)[1].lower()
        with open(fpath, 'rb') as f:
            if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                await bot.send_photo(uid, f)
            else:
                await bot.send_video(uid, f, supports_streaming=True)
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Ошибка Pinterest: {e}")
    finally:
        for f in glob.glob(f"{DOWNLOAD_DIR}/{uid}_*"): 
            try: os.remove(f)
            except: pass

@dp.message_handler(lambda m: "http" in m.text)
async def handle_others(message: types.Message):
    if re.search(PINTEREST_RE, message.text): return
    url = re.search(r'(https?://\S+)', message.text).group(1)
    user_urls[message.from_user.id] = url
    
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🎬 Видео", callback_data="get_video"),
        InlineKeyboardButton("🎵 Аудио", callback_data="get_audio")
    )
    await message.answer("🎯 Формат:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('get_'))
async def process_callback(callback: CallbackQuery):
    uid, mode = callback.from_user.id, callback.data.split('_')[1]
    url = user_urls.get(uid)
    if not url or user_locks.get(uid): return
    
    user_locks[uid] = True
    status = await callback.message.edit_text("⏳ Обработка видео для телефона...")

    try:
        fpath = await download_media(url, uid, mode)
        if not fpath: raise Exception("Ошибка скачивания")

        with open(fpath, 'rb') as f:
            if mode == "audio": await bot.send_audio(uid, f)
            else: await bot.send_video(uid, f, supports_streaming=True)
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Ошибка: {e}")
    finally:
        user_locks[uid] = False
        for f in glob.glob(f"{DOWNLOAD_DIR}/{uid}_*"):
            try: os.remove(f)
            except: pass

@dp.message_handler(commands=['ask'])
async def ai_ask(message: types.Message):
    query = message.get_args()
    if not query or not gemini_model: return
    status = await message.answer("🤔")
    res = await asyncio.to_thread(gemini_model.generate_content, query)
    await safe_edit(status, res.text)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
