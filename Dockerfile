FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Установка yt-dlp напрямую (свежая версия)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p downloads

CMD ["python", "-u", "bot.py"]
