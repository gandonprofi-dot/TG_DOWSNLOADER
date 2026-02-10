import os
import json
import asyncio
import glob
import aiohttp
import re
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_urls = {}
user_locks = {}

# --- ИНИЦИАЛИЗАЦИЯ GEMINI ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def upload_to_gofile(file_path):
    """Загрузка файла на GoFile, если он больше 50МБ"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.gofile.io/getServer") as r:
                server = (await r.json())['data']['server']
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f)
                async with session.post(f"https://{server}.gofile.io/uploadFile", data=data) as r:
                    res = await r.json()
                    return res['data']['downloadPage']
    except: return None

async def safe_edit(message: types.Message, text: str, kb=None):
    """Редактирование без ошибок 'MessageNotModified'"""
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except: pass

# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 **Бот готов к работе!**\n\n"
        "• Пришли ссылку на **YouTube, TikTok, Insta, Pinterest**\n"
        "• Напиши `/ask [вопрос]`, чтобы пообщаться с Gemini\n"
        "• Напиши `/summary [ссылка]`, чтобы кратко пересказать видео",
        parse_mode="Markdown"
    )

@dp.message_handler(commands=['ask'])
async def ai_ask(message: types.Message):
    query = message.get_args()
    if not query or not model:
        return await message.answer("Напиши вопрос после команды. Пример: `/ask как дела?`", parse_mode="Markdown")
    
    status = await message.answer("🤔 Думаю...")
    try:
        # Запускаем генерацию в отдельном потоке, чтобы не фризить бота
        response = await asyncio.to_thread(model.generate_content, query)
        await safe_edit(status, response.text[:4096])
    except Exception as e:
        await safe_edit(status, "❌ Ошибка ИИ. Попробуй позже.")

@dp.message_handler(commands=['summary'])
async def ai_summary(message: types.Message):
    url = message.get_args()
    video_id = re.search(r'(?:v=|be/|shorts/)([\w-]+)', url)
    if not video_id:
        return await message.answer("Пришли корректную ссылку на YouTube")

    status = await message.answer("📖 Читаю субтитры...")
    try:
        srv = YouTubeTranscriptApi.get_transcript(video_id.group(1), languages=['ru', 'en'])
        full_text = " ".join([t['text'] for t in srv])[:10000]
        
        prompt = f"Сделай краткое содержание этого видео: {full_text}"
        res = await asyncio.to_thread(model.generate_content, prompt)
        await safe_edit(status, f"📝 **Результат:**\n\n{res.text[:4000]}")
    except:
        await safe_edit(status, "❌ Не удалось получить субтитры или видео слишком длинное.")

# --- СКАЧИВАНИЕ (PINTEREST И ОСТАЛЬНОЕ) ---

@dp.message_handler(lambda m: "http" in m.text)
async def handle_links(message: types.Message):
    url = re.search(r'(https?://\S+)', message.text).group(1)
    user_urls[message.from_user.id] = url
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎬 Видео", callback_data="dl_video"),
        InlineKeyboardButton("🎵 Аудио", callback_data="dl_audio")
    )
    await message.answer("🎯 Что нужно сделать?", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('dl_'))
async def process_dl(callback: CallbackQuery):
    uid = callback.from_user.id
    mode = callback.data.split('_')[1]
    url = user_urls.get(uid)

    if not url: return await callback.answer("Ссылка потеряна, отправь еще раз.")
    if user_locks.get(uid): return await callback.answer("Подожди, я еще занят твоим прошлым запросом!")

    user_locks[uid] = True
    status = await callback.message.edit_text("⏳ Загрузка началась...")

    try:
        f_tmpl = f"{DOWNLOAD_DIR}/{uid}_%(id)s.%(ext)s"
        # Базовые настройки yt-dlp
        cmd = ["yt-dlp", "-o", f_tmpl, "--no-playlist", "--no-warnings"]
        
        if mode == "audio":
            cmd += ["-x", "--audio-format", "mp3"]
        else:
            # Для Pinterest и видео - лучший mp4
            cmd += ["-f", "best[ext=mp4]/best"]

        cmd.append(url)
        
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()

        files = glob.glob(f"{DOWNLOAD_DIR}/{uid}_*")
        if not files: raise Exception("Не удалось скачать файл.")

        fpath = files[0]
        size_mb = os.path.getsize(fpath) / (1024 * 1024)
        ext = os.path.splitext(fpath)[1].lower()

        if size_mb > 49:
            await safe_edit(status, "☁️ Файл большой, загружаю в облако...")
            link = await upload_to_gofile(fpath)
            await safe_edit(status, f"✅ Готово! Файл весит {size_mb:.1f} МБ.\n🔗 [Скачать файл]({link})")
        else:
            await safe_edit(status, "📤 Отправляю...")
            with open(fpath, 'rb') as f:
                if mode == "audio": await bot.send_audio(uid, f)
                elif ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']: await bot.send_photo(uid, f)
                else: await bot.send_video(uid, f, supports_streaming=True)
            await status.delete()

    except Exception as e:
        await safe_edit(status, f"❌ Ошибка: {str(e)[:100]}")
    finally:
        user_locks[uid] = False
        for f in glob.glob(f"{DOWNLOAD_DIR}/{uid}_*"):
            try: os.remove(f)
            except: pass

if __name__ == "__main__":
    print("🚀 Бот запущен!")
    executor.start_polling(dp, skip_updates=True)
