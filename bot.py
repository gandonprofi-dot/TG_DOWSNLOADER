import os
import json
import asyncio
import glob
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# =========================
# НАСТРОЙКИ
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_JSON = os.getenv("GDRIVE_JSON")

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
    """Синхронная загрузка в Google Drive"""
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
    """Асинхронная обёртка для загрузки в Google Drive"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor_pool, upload_to_drive_sync, file_path)

# =========================
# GOFILE
# =========================
async def upload_to_gofile(file_path):
    """Загрузка файла на GoFile"""
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем сервер
            async with session.get("https://api.gofile.io/getServer") as response:
                if response.status != 200:
                    raise Exception("Не удалось получить сервер GoFile")
                
                server_data = await response.json()
                if server_data['status'] != 'ok':
                    raise Exception("Ошибка API GoFile")
                
                server = server_data['data']['server']
            
            # Загружаем файл
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================
def extract_url_from_message(message: types.Message) -> str:
    """Извлекает URL из сообщения (текст или entity)"""
    # Вариант 1: Прямой текст
    if message.text:
        text = message.text.strip()
        # Проверяем что это не команда
        if not text.startswith('/'):
            # Проверяем что это похоже на URL
            if any(domain in text.lower() for domain in ['http://', 'https://', '.com', '.ru', '.org']):
                return text
    
    # Вариант 2: URL в entities
    if message.entities:
        for entity in message.entities:
            if entity.type == 'url':
                # Извлекаем URL из текста
                url = message.text[entity.offset:entity.offset + entity.length]
                return url
            elif entity.type == 'text_link':
                # URL в text_link
                return entity.url
    
    return None

def is_supported_url(url: str) -> bool:
    """Проверяет поддерживается ли URL"""
    if not url:
        return False
    
    supported_domains = [
        'youtube.', 'youtu.be', 
        'instagram.', 'insta',
        'tiktok.', 
        'facebook.', 'fb.watch', 'fb.com',
        'vk.com',
        'twitter.', 'x.com',
        'reddit.com',
        'twitch.tv'
    ]
    
    url_lower = url.lower()
    return any(domain in url_lower for domain in supported_domains)

async def cleanup_user_files(user_id: int):
    """Удаляет все файлы пользователя"""
    for f in glob.glob(f"{DOWNLOAD_DIR}/{user_id}_*"):
        try:
            os.remove(f)
            print(f"🗑️ Удалён файл: {f}")
        except Exception as e:
            print(f"⚠️ Не удалось удалить {f}: {e}")

def clear_user_state(user_id: int):
    """Очищает состояние пользователя"""
    if user_id in user_urls:
        del user_urls[user_id]
    if user_id in user_locks:
        del user_locks[user_id]

# =========================
# КОМАНДЫ
# =========================
@dp.message_handler(commands=["start", "help"])
async def start(message: types.Message):
    """Команда /start и /help"""
    user_id = message.from_user.id
    # Очищаем старое состояние при /start
    clear_user_state(user_id)
    await cleanup_user_files(user_id)
    
    await message.answer(
        "👋 <b>Привет! Я скачиваю видео из соцсетей</b>\n\n"
        "📱 <b>Поддерживаю:</b>\n"
        "• YouTube / YouTube Shorts\n"
        "• Instagram / Reels\n"
        "• TikTok\n"
        "• Facebook\n"
        "• VK, Twitter/X, Reddit, Twitch\n\n"
        "🎯 <b>Как пользоваться:</b>\n"
        "1. Отправь мне ссылку на видео\n"
        "2. Выбери формат (видео или аудио)\n"
        "3. Получи файл!\n\n"
        "💡 <b>Возможности:</b>\n"
        "🎬 Видео в лучшем качестве\n"
        "🎵 Только аудио\n"
        "☁️ Большие файлы → GoFile/Drive\n\n"
        "⚡ Быстро и просто!\n\n"
        "🔧 Команды:\n"
        "/cancel - отменить текущее скачивание",
        parse_mode="HTML"
    )

@dp.message_handler(commands=["cancel"])
async def cancel(message: types.Message):
    """Команда /cancel - отмена скачивания"""
    user_id = message.from_user.id
    clear_user_state(user_id)
    await cleanup_user_files(user_id)
    await message.answer(
        "✅ Скачивание отменено\n\n"
        "Можешь отправить новую ссылку"
    )

# =========================
# ОБРАБОТКА ССЫЛКИ
# =========================
@dp.message_handler(content_types=['text'])
async def handle_url(message: types.Message):
    """Обработка текстовых сообщений с URL"""
    print(f"\n=== НОВОЕ СООБЩЕНИЕ ===")
    print(f"User ID: {message.from_user.id}")
    print(f"Username: {message.from_user.username}")
    print(f"Text: {message.text}")
    
    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        print("❌ Команда игнорируется")
        return
    
    # Извлекаем URL
    url = extract_url_from_message(message)
    print(f"Extracted URL: {url}")
    
    if not url:
        await message.answer(
            "❌ Не могу найти ссылку в сообщении\n\n"
            "Отправь мне прямую ссылку на видео"
        )
        return
    
    # Проверяем поддержку
    if not is_supported_url(url):
        await message.answer(
            "❌ Эта платформа не поддерживается\n\n"
            "📱 Поддерживаю:\n"
            "• YouTube\n"
            "• Instagram\n"
            "• TikTok\n"
            "• Facebook\n"
            "• VK, Twitter, Reddit, Twitch"
        )
        return
    
    user_id = message.from_user.id
    
    # Очищаем старые файлы и блокировку при новой ссылке
    await cleanup_user_files(user_id)
    if user_id in user_locks:
        del user_locks[user_id]
    
    # Сохраняем новый URL
    user_urls[user_id] = url
    print(f"✅ URL сохранён для пользователя {user_id}")
    
    # Создаём меню выбора
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎬 Видео (лучшее)", callback_data="quality_best"),
        InlineKeyboardButton("🎵 Аудио", callback_data="quality_audio")
    )
    
    await message.answer(
        "🎯 <b>Выбери формат:</b>\n\n"
        "🎬 <b>Видео</b> — максимальное качество\n"
        "🎵 <b>Аудио</b> — только звук",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

# =========================
# ОБРАБОТКА ВЫБОРА КАЧЕСТВА
# =========================
@dp.callback_query_handler(lambda c: c.data.startswith('quality_'))
async def process_quality(callback: CallbackQuery):
    """Обработка выбора качества"""
    # Отвечаем на callback
    await callback.answer()
    
    user_id = callback.from_user.id
    quality = callback.data.replace('quality_', '')
    
    print(f"\n=== ОБРАБОТКА КАЧЕСТВА ===")
    print(f"User ID: {user_id}")
    print(f"Quality: {quality}")
    
    # Проверяем блокировку
    if user_locks.get(user_id):
        await callback.answer("⏳ Подожди, предыдущее скачивание ещё идёт!", show_alert=True)
        return
    
    # Блокируем пользователя
    user_locks[user_id] = True
    
    try:
        # Получаем URL
        url = user_urls.get(user_id)
        
        if not url:
            await callback.message.edit_text(
                "❌ Ссылка потерялась. Отправь её заново.\n\n"
                "Нажми /start"
            )
            # Очищаем всё состояние только при отсутствии URL
            if user_id in user_locks:
                del user_locks[user_id]
            return
        
        print(f"URL: {url}")
        
        # Обновляем сообщение
        await callback.message.edit_text("⏳ Скачиваю...")
        
        # Определяем параметры скачивания
        template = f"{DOWNLOAD_DIR}/{user_id}_%(id)s.%(ext)s"
        is_instagram = "instagram.com" in url.lower() or "insta" in url.lower()
        is_shorts = "shorts" in url.lower() or "youtu.be" in url.lower()
        
        # Формат для yt-dlp
        if quality == "audio":
            format_str = "bestaudio/best"
            merge_format = None
        else:  # best
            if is_instagram:
                format_str = "best"
            elif is_shorts:
                format_str = "best"
            else:
                format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            merge_format = "mp4"
        
        # Формируем команду
        cmd = ["yt-dlp", "--no-playlist"]
        
        if is_instagram:
            cmd.extend([
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ])
        
        cmd.extend(["-f", format_str])
        
        if merge_format:
            cmd.extend(["--merge-output-format", merge_format])
        
        cmd.extend(["-o", template, url])
        
        print(f"Команда: {' '.join(cmd)}")
        
        # Скачиваем
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
        except asyncio.TimeoutError:
            process.kill()
            await callback.message.edit_text("❌ Таймаут скачивания (10 минут)")
            return
        
        # Проверяем результат
        if process.returncode != 0:
            error = stderr.decode('utf-8', errors='ignore')
            print(f"❌ Ошибка yt-dlp: {error[:500]}")
            
            if "private" in error.lower() or "login" in error.lower():
                await callback.message.edit_text("❌ Видео приватное или требует авторизации")
            elif "unavailable" in error.lower() or "not available" in error.lower():
                await callback.message.edit_text("❌ Видео недоступно или удалено")
            elif "no video formats" in error.lower():
                await callback.message.edit_text("❌ Не найдено видео для скачивания")
            else:
                await callback.message.edit_text(
                    "❌ Не удалось скачать видео\n\n"
                    "Проверь ссылку и попробуй снова"
                )
            return
        
        # Ищем скачанный файл
        files = glob.glob(f"{DOWNLOAD_DIR}/{user_id}_*")
        if not files:
            await callback.message.edit_text("❌ Файл не найден после скачивания")
            return
        
        file_path = files[0]
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        print(f"✅ Файл скачан: {file_path} ({size_mb:.1f} MB)")
        
        # Проверяем наличие видеопотока
        has_video = False
        if quality != "audio":
            try:
                probe = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0",
                    file_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await probe.communicate()
                has_video = b"video" in stdout
                print(f"Видеопоток: {has_video}")
            except Exception as e:
                print(f"⚠️ Не удалось проверить видеопоток: {e}")
                has_video = True  # По умолчанию считаем что есть
        
        # Если запросили видео но есть только аудио
        if quality == "best" and not has_video:
            await callback.message.edit_text(
                f"⚠️ Видео недоступно, скачалось только аудио\n"
                f"📤 Отправляю аудио ({size_mb:.1f} MB)..."
            )
            
            with open(file_path, "rb") as audio:
                await callback.message.answer_audio(
                    audio,
                    caption=f"🎵 Аудио | {size_mb:.1f} MB"
                )
            
            try:
                await callback.message.delete()
            except:
                pass
            return
        
        # Отправляем аудио
        if quality == "audio":
            await callback.message.edit_text(f"📤 Отправляю аудио ({size_mb:.1f} MB)...")
            
            with open(file_path, "rb") as audio:
                await callback.message.answer_audio(
                    audio,
                    caption=f"🎵 Аудио | {size_mb:.1f} MB"
                )
            
            try:
                await callback.message.delete()
            except:
                pass
        
        # Отправляем видео (до 2 GB)
        elif size_mb <= TELEGRAM_VIDEO_LIMIT:
            await callback.message.edit_text(f"📤 Отправляю видео ({size_mb:.1f} MB)...")
            
            with open(file_path, "rb") as video:
                await callback.message.answer_video(
                    video,
                    caption=f"🎬 Лучшее качество | {size_mb:.1f} MB",
                    supports_streaming=True
                )
            
            try:
                await callback.message.delete()
            except:
                pass
        
        # Загружаем на облако (больше 2 GB)
        else:
            await callback.message.edit_text(
                f"⚠️ Файл слишком большой: {size_mb:.1f} MB\n"
                f"☁️ Загружаю на GoFile..."
            )
            
            try:
                link = await upload_to_gofile(file_path)
                
                await callback.message.edit_text(
                    f"✅ <b>Загружено на GoFile!</b>\n\n"
                    f"📦 Размер: {size_mb:.1f} MB\n"
                    f"🔗 Ссылка:\n<code>{link}</code>\n\n"
                    f"💡 Нажми на ссылку чтобы скопировать",
                    parse_mode="HTML"
                )
            
            except Exception as gofile_error:
                print(f"❌ GoFile ошибка: {gofile_error}")
                
                # Пробуем Google Drive
                if drive:
                    await callback.message.edit_text(
                        f"⚠️ GoFile недоступен\n"
                        f"☁️ Загружаю в Google Drive..."
                    )
                    
                    try:
                        link = await upload_to_drive(file_path)
                        await callback.message.edit_text(
                            f"✅ <b>Загружено в Google Drive!</b>\n\n"
                            f"📦 Размер: {size_mb:.1f} MB\n"
                            f"🔗 <code>{link}</code>",
                            parse_mode="HTML"
                        )
                    except Exception as drive_error:
                        print(f"❌ Google Drive ошибка: {drive_error}")
                        await callback.message.edit_text(
                            f"❌ Не удалось загрузить файл\n\n"
                            f"Размер: {size_mb:.1f} MB (слишком большой)\n"
                            f"Попробуй скачать напрямую: {url}"
                        )
                else:
                    await callback.message.edit_text(
                        f"❌ Файл слишком большой: {size_mb:.1f} MB\n"
                        f"Лимит Telegram: {TELEGRAM_VIDEO_LIMIT} MB\n\n"
                        f"Скачай напрямую: {url}"
                    )
    
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            await callback.message.edit_text(
                f"❌ Произошла ошибка\n\n"
                f"Попробуй позже или отправь другую ссылку"
            )
        except:
            await callback.message.answer("❌ Произошла критическая ошибка")
    
    finally:
        # Очистка только файлов и блокировки
        print("🧹 Очистка файлов...")
        await cleanup_user_files(user_id)
        # Снимаем блокировку
        if user_id in user_locks:
            del user_locks[user_id]
        # URL НЕ удаляем - пусть остаётся для повторных попыток
        print("✅ Очистка завершена")

# =========================
# ОБРАБОТКА ОШИБОК
# =========================
@dp.errors_handler()
async def errors_handler(update, exception):
    """Глобальный обработчик ошибок"""
    print(f"❌ Ошибка: {exception}")
    import traceback
    traceback.print_exc()
    return True

# =========================
# ЗАПУСК
# =========================
async def on_startup(dp):
    """Действия при запуске бота"""
    print("🔧 Очистка webhook...")
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook очищен")

async def on_shutdown(dp):
    """Действия при остановке бота"""
    print("🧹 Очистка сессий...")
    await bot.close()
    print("✅ Бот остановлен корректно")

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 BOT STARTING")
    print("=" * 50)
    print(f"🎬 Лимит Telegram: {TELEGRAM_VIDEO_LIMIT} MB")
    print(f"☁️ Google Drive: {'✅ Включен' if drive else '❌ Отключен'}")
    print(f"📁 Директория: {DOWNLOAD_DIR}")
    print("=" * 50)
    
    try:
        executor.start_polling(
            dp, 
            skip_updates=True,
            on_startup=on_startup,
            on_shutdown=on_shutdown
        )
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
    finally:
        executor_pool.shutdown(wait=True)
        print("👋 Завершение работы")
