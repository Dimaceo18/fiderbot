# Используем официальный образ Python 3.11 (стабильная версия)
FROM python:3.11-slim

# Устанавливаем системные зависимости, необходимые для Pillow
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все остальные файлы проекта (бота, шрифты) в контейнер
COPY . .

# Указываем команду для запуска бота
CMD ["python", "bot.py"]
