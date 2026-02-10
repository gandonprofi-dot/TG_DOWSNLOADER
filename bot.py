import os
import asyncio
import glob
import re
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
# Обновленный импорт Google AI
from google import genai 

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_urls = {}
user_locks = {}

# Инициализация нового клиента Gemini
client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("✅ Новый клиент Gemini готов")
    except Exception as e:
        logging.error(f"❌ Ошибка ИИ: {e}")

# ... (остальные функции скачивания остаются такими же, как в прошлом ответе) ...

@dp.message_handler(commands=['ask'])
async def ask(message: types.Message):
    q = message.get_args()
    if not q or not client: return
    
    status = await message.answer("🤔 Думаю...")
    try:
        # Новый способ вызова Gemini
        response = client.models.generate_content(
            model="gemini-1.5-flash", contents=q
        )
        await status.edit_text(response.text)
    except Exception as e:
        await status.edit_text(f"Ошибка ИИ: {e}")

# Остальной код (handle_urls, handle_pinterest) копируй из прошлого моего сообщения.

if __name__ == "__main__":
    # skip_updates=True помогает при перезапусках игнорировать старые сообщения
    executor.start_polling(dp, skip_updates=True)
