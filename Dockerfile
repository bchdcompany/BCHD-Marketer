FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для Playwright
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Копируем только requirements.txt первым — Docker кэширует этот слой
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем браузер для Playwright
RUN playwright install chromium --with-deps 2>/dev/null || true

# Cache bust — инвалидирует кеш COPY . . при изменении
ARG CACHE_BUST=2

# Копируем код отдельным слоем
COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "bot.py"]
