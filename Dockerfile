FROM python:3.11-slim

# Системные зависимости
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -U yt-dlp && \
    pip install --no-cache-dir -r requirements.txt

# Код бота
COPY bot.py .

# Папка для скачивания
RUN mkdir -p downloads

# Запуск
CMD ["python", "-u", "bot.py"]
