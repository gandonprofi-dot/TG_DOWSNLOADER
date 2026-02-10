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
else:
    gemini_model = None

# Регулярка для Pinterest
PINTEREST_RE = r'(https?://(?:www\.)?(?:pinterest\.com/pin/|pin\.it)/?\S+)'

async def safe_edit(message: types.Message, text: str):
    try: await message.edit_text(text, parse_mode="Markdown")
    except: pass

# --- ФУНКЦИЯ СКАЧИВАНИЯ ---
async def download_media(url, uid, mode="video"):
    f_tmpl = f"{DOWNLOAD_DIR}/{uid}_%(id)s.%(ext)s"
    
    # Исправленные параметры для звука
    if mode == "audio":
        cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", f_tmpl, url]
    else:
        # Мерджим лучшее видео и аудио в один mp4
        cmd = [
            "yt-dlp", 
            "-f", "bestvideo+bestaudio/best", 
            "--merge-output-format", "mp4",
            "-o", f_tmpl, 
            "--no-playlist",
            url
        ]
    
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    
    files = glob.glob(f"{DOWNLOAD_DIR}/{uid}_*")
    return files[0] if files else None

# --- ОБРАБОТКА PINTEREST (АВТОМАТИЧЕСКИ) ---
@dp.message_handler(regexp=PINTEREST_RE)
async def handle_pinterest(message: types.Message):
    url = re.search(PINTEREST_RE, message.text).group(1)
    uid = message.from_user.id
    
    status = await message.answer("🖼 Обрабатываю Pinterest...")
    
    try:
        fpath = await download_media(url, uid)
        if not fpath: raise Exception("Не скачалось")
        
        ext = os.path.splitext(fpath)[1].lower()
        with open(fpath, 'rb') as f:
            if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                await bot.send_photo(uid, f, caption="Pinterest Photo")
            else:
                await bot.send_video(uid, f, caption="Pinterest Video")
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Ошибка Pinterest: {e}")
    finally:
        for f in glob.glob(f"{DOWNLOAD_DIR}/{uid}_*"): os.remove(f)

# --- ОБРАБОТКА ОСТАЛЬНЫХ ССЫЛОК (С ВЫБОРОМ) ---
@dp.message_handler(lambda m: "http" in m.text)
async def handle_others(message: types.Message):
    # Если это не пинтерест, предлагаем выбор
    if re.search(PINTEREST_RE, message.text): return
    
    url = re.search(r'(https?://\S+)', message.text).group(1)
    user_urls[message.from_user.id] = url
    
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🎬 Видео (со звуком)", callback_data="get_video"),
        InlineKeyboardButton("🎵 Аудио", callback_data="get_audio")
    )
    await message.answer("🎯 Выберите формат:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('get_'))
async def process_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    mode = "audio" if "audio" in callback.data else "video"
    url = user_urls.get(uid)
    
    if not url: return await callback.answer("Ссылка не найдена")
    if user_locks.get(uid): return await callback.answer("Я уже качаю!")

    user_locks[uid] = True
    status = await callback.message.edit_text("⏳ Загрузка медиа...")

    try:
        fpath = await download_media(url, uid, mode)
        if not fpath: raise Exception("Ошибка при скачивании")

        size_mb = os.path.getsize(fpath) / (1024*1024)
        
        if size_mb > 50:
            await safe_edit(status, "⚠️ Файл слишком большой для Telegram. Используйте GoFile...")
            # Тут можно вызвать функцию upload_to_gofile из прошлых ответов
        else:
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

# --- GEMINI ЧАТ ---
@dp.message_handler(commands=['ask'])
async def ai_ask(message: types.Message):
    query = message.get_args()
    if not query or not gemini_model: return
    
    status = await message.answer("🤔...")
    try:
        res = await asyncio.to_thread(gemini_model.generate_content, query)
        await safe_edit(status, res.text)
    except: await safe_edit(status, "Ошибка ИИ")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
