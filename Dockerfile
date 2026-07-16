FROM python:3.11-slim

WORKDIR /app

# Копируем только requirements.txt первым — Docker кэширует этот слой
# и не переустанавливает пакеты если requirements.txt не изменился.
# Это сокращает деплой с 6-10 минут до 30-60 секунд при изменении .py файлов.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код отдельным слоем — только он меняется при каждом деплое
COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "bot.py"]
