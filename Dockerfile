# Базовый образ Python
FROM python:3.11-slim

# Устанавливаем системные шрифты с кириллицей (делаем это ОДИН раз при сборке образа)
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv

# Рабочая директория
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python-пакеты
RUN pip install --no-cache-dir -r requirements.txt

# Копируем ТОЛЬКО Montserrat шрифты
COPY Montserrat-Bold.ttf /app/
COPY Montserrat-Medium.ttf /app/
COPY Montserrat-Regular.ttf /app/
COPY Montserrat-Black.ttf /app/

# Копируем код
COPY main.py .

# Открываем порт
EXPOSE 3000

# Запуск
CMD ["python", "main.py"]
