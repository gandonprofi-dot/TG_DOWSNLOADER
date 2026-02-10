import os
import json
import asyncio
import glob
import aiohttp
import re
from concurrent.futures import ThreadPoolExecutor
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi

# =========================
# НАСТРОЙКИ
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_JSON = os.getenv("GDRIVE_JSON")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TELEGRAM_VIDEO_LIMIT = 2000  # 2 GB
DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
executor_pool = ThreadPoolExecutor(max_workers=3)

# Храним выбор пользователей {user_id: url}
user_urls = {}
# Блокировка для предотвращения одновременных скачиваний
user_locks = {}

# =========================
# GEMINI AI
# =========================
gemini_model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        print("✅ Gemini AI включен")
    except Exception as e:
        print(f"⚠️ Gemini AI отключен: {e}")

# =========================
# GOOGLE DRIVE
# =========================
drive = None
if GDRIVE_JSON:
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GDRIVE_JSON),
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        drive = build("drive", "v3", credentials=creds)
        print("✅ Google Drive включен")
    except Exception as e:
        print(f"⚠️ Google Drive отключен: {e}")

def upload_to_drive_sync(file_path):
    if not drive:
        raise Exception("Google Drive не настроен")
    
    file_metadata = {"name": os.path.basename(file_path)}
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    
    file = drive.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    
    drive.permissions().create(
        fileId=file['id'],
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    
    return f"https://drive.google.com/file/d/{file['id']}/view"

async def upload_to_drive(file_path):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor_pool, upload_to_drive_sync, file_path)

# =========================
# GOFILE
# =========================
async def upload_to_gofile(file_path):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.gofile.io/getServer") as response:
                if response.status != 200:
                    raise Exception("Не удалось получить сервер GoFile")
                
                server_data = await response.json()
                if server_data['status'] != 'ok':
                    raise Exception("Ошибка API GoFile")
                
                server = server_data['data']['server']
            
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=os.path.basename(file_path))
                
                async with session.post(
                    f"https://{server}.gofile.io/uploadFile",
                    data=data
                ) as response:
                    if response.status != 200:
                        raise Exception("Ошибка загрузки на GoFile")
                    
                    result = await response.json()
                    if result['status'] != 'ok':
                        raise Exception("Ошибка ответа GoFile")
                    
                    return result['data']['downloadPage']
    
    except Exception as e:
        raise Exception(f"GoFile ошибка: {str(e)}")

# =========================
# СЖАТИЕ ВИДЕО
# =========================
async def compress_video(input_path, output_path, target_mb):
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
            stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await probe.communicate()
        duration = float(stdout.decode().strip())
        
        target_bits = target_mb * 1024 * 1024 * 8 * 0.95
        bitrate = max(int(target_bits / duration) - 128000, 500000)
        
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", input_path,
            "-c:v", "libx264",
            "-b:v", str(bitrate),
            "-preset", "medium",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        
        await process.communicate()
        return process.returncode == 0
    
    except Exception:
        return False

# =========================
# КОМАНДЫ
# =========================
@dp.message_handler(commands=["start", "help"])
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я многофункциональный бот!\n\n"
        "📥 **Скачивание видео:**\n"
        "Отправь ссылку на видео\n"
        "• YouTube / Shorts\n"
        "• Instagram / Reels  \n"
        "• TikTok / Facebook\n\n"
        "🤖 **AI команды:**\n"
        "/ask [вопрос] — спросить AI\n"
        "/search [запрос] — найти видео на YouTube\n"
        "/summary [ссылка] — краткое содержание\n\n"
        "⚡ Быстро, умно, удобно!",
        parse_mode="Markdown"
    )

# =========================
# AI ЧАТ
# =========================
@dp.message_handler(commands=["ask"])
async def ai_chat(message: types.Message):
    if not gemini_model:
        await message.answer("❌ AI временно недоступен")
        return
    
    # Получаем вопрос
    question = message.text.replace("/ask", "").strip()
    
    if not question:
        await message.answer("💡 Используй: /ask [твой вопрос]\n\nПример:\n/ask Как готовить плов?")
        return
    
    status = await message.answer("🤔 Думаю...")
    
    try:
        # Генерируем ответ
        response = gemini_model.generate_content(question)
        answer = response.text
        
        # Telegram лимит 4096 символов
        if len(answer) > 4000:
            answer = answer[:4000] + "...\n\n_Ответ обрезан_"
        
        await status.edit_text(answer, parse_mode="Markdown")
    
    except Exception as e:
        print(f"AI error: {e}")
        await status.edit_text("❌ Ошибка AI. Попробуй переформулировать вопрос.")

# =========================
# ПОИСК ВИДЕО НА YOUTUBE
# =========================
@dp.message_handler(commands=["search"])
async def search_youtube(message: types.Message):
    query = message.text.replace("/search", "").strip()
    
    if not query:
        await message.answer("🔍 Используй: /search [запрос]\n\nПример:\n/search как готовить плов")
        return
    
    status = await message.answer("🔎 Ищу...")
    
    try:
        # Используем YouTube Data API через Gemini или обычный поиск
        async with aiohttp.ClientSession() as session:
            # Простой поиск через YouTube
            search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
            
            async with session.get(search_url) as resp:
                html = await resp.text()
                
                # Простой парсинг video ID
                video_ids = re.findall(r'"videoId":"([^"]+)"', html)[:5]
                
                if not video_ids:
                    await status.edit_text("❌ Ничего не найдено")
                    return
                
                # Формируем результаты с кнопками
                keyboard = InlineKeyboardMarkup(row_width=1)
                
                for i, vid_id in enumerate(video_ids[:5], 1):
                    url = f"https://youtu.be/{vid_id}"
                    keyboard.add(
                        InlineKeyboardButton(
                            f"📹 Видео {i}",
                            url=url
                        ),
                        InlineKeyboardButton(
                            f"⬇️ Скачать видео {i}",
                            callback_data=f"dl_{vid_id}"
                        )
                    )
                
                await status.edit_text(
                    f"🔍 Найдено по запросу: *{query}*\n\n"
                    f"Выбери видео:",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
    
    except Exception as e:
        print(f"Search error: {e}")
        await status.edit_text("❌ Ошибка поиска. Попробуй ещё раз.")

# =========================
# КРАТКОЕ СОДЕРЖАНИЕ ВИДЕО
# =========================
@dp.message_handler(commands=["summary"])
async def video_summary(message: types.Message):
    if not gemini_model:
        await message.answer("❌ AI временно недоступен")
        return
    
    url = message.text.replace("/summary", "").strip()
    
    if not url:
        await message.answer("📝 Используй: /summary [ссылка на видео]\n\nПример:\n/summary https://youtu.be/abc123")
        return
    
    # Извлекаем video ID
    video_id = None
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&\s]+)',
        r'youtube\.com\/shorts\/([^&\s]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            break
    
    if not video_id:
        await message.answer("❌ Не могу распознать YouTube ссылку")
        return
    
    status = await message.answer("📖 Читаю субтитры...")
    
    try:
        # Получаем субтитры
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ru', 'en'])
        
        # Собираем текст
        full_text = " ".join([entry['text'] for entry in transcript_list])
        
        # Ограничиваем длину для AI
        if len(full_text) > 10000:
            full_text = full_text[:10000] + "..."
        
        await status.edit_text("🤖 Делаю краткое содержание...")
        
        # Генерируем саммари через AI
        prompt = f"Сделай краткое содержание этого видео на русском языке. Выдели главные моменты. Текст:\n\n{full_text}"
        
        response = gemini_model.generate_content(prompt)
        summary = response.text
        
        if len(summary) > 4000:
            summary = summary[:4000] + "..."
        
        await status.edit_text(
            f"📝 **Краткое содержание:**\n\n{summary}\n\n🔗 {url}",
            parse_mode="Markdown"
        )
    
    except Exception as e:
        error_msg = str(e)
        if "Subtitles are disabled" in error_msg or "No transcripts" in error_msg:
            await status.edit_text("❌ У этого видео нет субтитров")
        else:
            print(f"Summary error: {e}")
            await status.edit_text("❌ Не удалось получить содержание")

# Callback для скачивания из поиска
@dp.callback_query_handler(lambda c: c.data.startswith('dl_'))
async def download_from_search(callback: CallbackQuery):
    await callback.answer()
    
    video_id = callback.data.replace('dl_', '')
    url = f"https://youtu.be/{video_id}"
    
    # Сохраняем URL
    user_id = callback.from_user.id
    user_urls[user_id] = url
    
    # Показываем меню качества
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎬 Видео (лучшее)", callback_data="quality_best"),
        InlineKeyboardButton("🎵 Аудио", callback_data="quality_audio")
    )
    
    await callback.message.answer(
        f"📹 Видео: {url}\n\n🎯 Выбери формат:",
        reply_markup=keyboard
    )

# =========================
# ОБРАБОТКА ССЫЛКИ
# =========================
@dp.message_handler(content_types=['text'])
async def handle_url(message: types.Message):
    print(f"=== NEW MESSAGE ===")
    print(f"User ID: {message.from_user.id}")
    print(f"Text: {message.text}")
    print(f"Entities: {message.entities}")
    
    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        print("Ignoring command")
        return
    
    # Получаем URL из текста или из entities (когда шарят через "Поделиться")
    url = None
    
    # Вариант 1: Прямой текст
    if message.text:
        url = message.text.strip()
        print(f"URL from text: {url}")
    
    # Вариант 2: URL в entities (когда делятся через кнопку)
    if message.entities:
        print(f"Found {len(message.entities)} entities")
        for i, entity in enumerate(message.entities):
            print(f"Entity {i}: type={entity.type}, offset={entity.offset}, length={entity.length}")
            
            if entity.type in ['url', 'text_link']:
                if entity.type == 'url':
                    # Извлекаем URL из текста
                    extracted_url = message.text[entity.offset:entity.offset + entity.length]
                    url = extracted_url
                    print(f"URL from entity (url): {url}")
                elif entity.type == 'text_link':
                    # URL в text_link
                    url = entity.url
                    print(f"URL from entity (text_link): {url}")
                break
    
    if not url:
        print("No URL found!")
        await message.answer("❌ Не могу найти ссылку в сообщении")
        return
    
    user_id = message.from_user.id
    
    # Проверяем что это похоже на URL
    supported_domains = ['youtube.', 'youtu.be', 'instagram.', 'insta', 'tiktok.', 'facebook.', 'fb.watch', 'fb.com', 'vk.com', 'twitter.', 'x.com', 'http']
    is_supported = any(domain in url.lower() for domain in supported_domains)
    
    print(f"Is supported URL: {is_supported}")
    
    if not is_supported:
        await message.answer("❌ Это не похоже на ссылку на видео\nПоддерживаю: YouTube, Instagram, TikTok, Facebook")
        return
    
    # Сохраняем URL пользователя
    user_urls[user_id] = url
    print(f"✅ Saved URL for user {user_id}: {url}")
    
    # Создаём меню выбора качества
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎬 Видео (лучшее)", callback_data="quality_best"),
        InlineKeyboardButton("🎵 Аудио", callback_data="quality_audio")
    )
    
    await message.answer(
        "🎯 Выбери формат:",
        reply_markup=keyboard
    )

# =========================
# ОБРАБОТКА ВЫБОРА КАЧЕСТВА
# =========================
@dp.callback_query_handler(lambda c: c.data.startswith('quality_'))
async def process_quality(callback: CallbackQuery):
    # ВАЖНО: отвечаем на callback сразу!
    await callback.answer()
    
    user_id = callback.from_user.id
    quality = callback.data.replace('quality_', '')
    
    # Проверяем что пользователь не скачивает уже
    if user_locks.get(user_id):
        await callback.answer("⏳ Подожди, предыдущее скачивание ещё идёт!", show_alert=True)
        return
    
    # Блокируем пользователя
    user_locks[user_id] = True
    
    try:
        # Получаем URL пользователя
        url = user_urls.get(user_id)
        print(f"Retrieved URL for user {user_id}: {url}")  # Для отладки
        
        if not url:
            await callback.message.edit_text(
                "❌ Ссылка потерялась. Отправьте заново.\n\n"
                "Нажмите /start"
            )
            # Снимаем блокировку
            if user_id in user_locks:
                del user_locks[user_id]
            return
        
        # Удаляем меню с кнопками
        try:
            await callback.message.edit_text("⏳ Скачиваю...")
        except:
            # Если не получилось отредактировать - отправляем новое
            await callback.message.answer("⏳ Скачиваю...")
        
        template = f"{DOWNLOAD_DIR}/{user_id}_%(id)s.%(ext)s"
        
        # Определяем платформу
        is_instagram = "instagram.com" in url.lower()
        is_shorts = "shorts" in url.lower() or "youtu.be" in url.lower()
        
        # Формат для yt-dlp в зависимости от качества
        if quality == "audio":
            # Только аудио
            format_str = "bestaudio/best"
        elif quality == "360":
            # 360p с запасными вариантами
            format_str = "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best"
        elif quality == "720":
            # 720p с запасными вариантами
            format_str = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        elif quality == "1080":
            # 1080p с запасными вариантами
            format_str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        else:  # best
            # Лучшее качество с запасными вариантами
            format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        
        # Команда для yt-dlp
        if is_instagram:
            cmd = [
                "yt-dlp", "--no-playlist",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "-f", format_str if quality != "best" else "best",
                "-o", template, url
            ]
        elif is_shorts:
            # Для Shorts упрощённый формат
            cmd = [
                "yt-dlp",
                "-f", "best" if quality == "best" else format_str,
                "--no-playlist",
                "-o", template, url
            ]
        else:
            # Обычное видео
            cmd = [
                "yt-dlp",
                "-f", format_str,
                "--merge-output-format", "mp4" if quality != "audio" else "m4a",
                "--no-playlist",
                "-o", template, url
            ]
        
        # Скачиваем
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
        
        if process.returncode != 0:
            error = stderr.decode('utf-8', errors='ignore')
            print(f"Ошибка для {url}: {error[:500]}")
            
            if "private" in error.lower() or "login" in error.lower():
                await callback.message.edit_text("❌ Видео приватное или требует авторизации")
            elif "unavailable" in error.lower():
                await callback.message.edit_text("❌ Видео недоступно или удалено")
            else:
                await callback.message.edit_text("❌ Не удалось скачать\nПроверьте ссылку")
            return
        
        # Ищем файл
        files = glob.glob(f"{DOWNLOAD_DIR}/{user_id}_*")
        if not files:
            await callback.message.edit_text("❌ Файл не найден")
            return
        
        file_path = files[0]
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        # Проверяем есть ли видео поток в файле
        has_video = False
        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                file_path,
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await probe.communicate()
            has_video = b"video" in stdout
        except:
            # Если не удалось проверить - считаем что видео есть
            has_video = True
        
        # Если НЕТ видео но пользователь выбрал видео качество - отправляем как аудио
        if not has_video and quality != "audio":
            await callback.message.edit_text(
                f"⚠️ Видео недоступно, скачалось только аудио\n"
                f"📤 Отправляю аудио ({size_mb:.1f} MB)..."
            )
            
            with open(file_path, "rb") as audio:
                await callback.message.answer_audio(
                    audio,
                    caption=f"🎵 Аудио (видео недоступно) | {size_mb:.1f} MB"
                )
            
            await callback.message.delete()
            return
        
        # Если аудио - отправляем как аудио
        if quality == "audio":
            await callback.message.edit_text(f"📤 Отправляю аудио ({size_mb:.1f} MB)...")
            
            with open(file_path, "rb") as audio:
                await callback.message.answer_audio(
                    audio,
                    caption=f"🎵 Аудио | {size_mb:.1f} MB"
                )
            
            await callback.message.delete()
        
        # До 2 GB - отправляем как видео
        elif size_mb <= TELEGRAM_VIDEO_LIMIT:
            # Конвертируем в правильный формат если нужно
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext not in ['.mp4']:
                await callback.message.edit_text(f"🔄 Конвертирую в MP4 ({size_mb:.1f} MB)...")
                
                converted_path = f"{DOWNLOAD_DIR}/{user_id}_converted.mp4"
                
                convert_cmd = [
                    "ffmpeg", "-i", file_path,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    "-y", converted_path
                ]
                
                conv_process = await asyncio.create_subprocess_exec(
                    *convert_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE
                )
                
                await conv_process.communicate()
                
                if conv_process.returncode == 0 and os.path.exists(converted_path):
                    file_path = converted_path
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
            
            await callback.message.edit_text(f"📤 Отправляю ({size_mb:.1f} MB)...")
            
            with open(file_path, "rb") as video:
                await callback.message.answer_video(
                    video,
                    caption=f"🎬 {quality.upper()} | {size_mb:.1f} MB",
                    supports_streaming=True
                )
            
            await callback.message.delete()
        
        # Больше 2 GB - GoFile
        else:
            await callback.message.edit_text(f"☁️ Загружаю на GoFile ({size_mb:.1f} MB)...")
            
            try:
                link = await upload_to_gofile(file_path)
                
                await callback.message.edit_text(
                    f"✅ Загружено на GoFile!\n\n"
                    f"📦 Качество: {quality.upper()}\n"
                    f"📦 Размер: {size_mb:.1f} MB\n"
                    f"🔗 Ссылка:\n{link}\n\n"
                    f"💡 Оригинальное качество"
                )
            
            except Exception as gofile_error:
                print(f"GoFile error: {gofile_error}")
                
                if drive:
                    await callback.message.edit_text(f"☁️ Загружаю в Google Drive ({size_mb:.1f} MB)...")
                    
                    try:
                        link = await upload_to_drive(file_path)
                        await callback.message.edit_text(
                            f"✅ Загружено в Google Drive!\n\n"
                            f"📦 Размер: {size_mb:.1f} MB\n"
                            f"🔗 {link}"
                        )
                    except Exception:
                        await callback.message.edit_text(
                            f"❌ Не удалось загрузить\n"
                            f"Скачай напрямую: {url}"
                        )
                else:
                    await callback.message.edit_text(
                        f"❌ Файл слишком большой: {size_mb:.1f} MB\n"
                        f"Скачай напрямую: {url}"
                    )
    
    except asyncio.TimeoutError:
        await callback.message.edit_text("❌ Таймаут (10 мин)")
    
    except asyncio.TimeoutError:
        await callback.message.edit_text("❌ Таймаут (10 мин)")
    
    except Exception as e:
        print(f"Ошибка: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
    
    finally:
        # Удаляем файлы
        for f in glob.glob(f"{DOWNLOAD_DIR}/{user_id}_*"):
            try:
                os.remove(f)
            except:
                pass
        
        # Очищаем сохранённый URL
        if user_id in user_urls:
            del user_urls[user_id]
        
        # Снимаем блокировку
        if user_id in user_locks:
            del user_locks[user_id]

# =========================
# ЗАПУСК
# =========================
if __name__ == "__main__":
    print("🚀 Бот запущен с выбором качества!")
    print(f"🎬 Лимит: {TELEGRAM_VIDEO_LIMIT} MB")
    print(f"☁️ Drive: {'Да' if drive else 'Нет'}")
    
    try:
        executor.start_polling(dp, skip_updates=True)
    finally:
        executor_pool.shutdown(wait=True)
