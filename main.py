import os
import sys
import json
import base64
import sqlite3
from datetime import datetime
from io import BytesIO
import textwrap

import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import dashscope
from dashscope import MultiModalConversation
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import requests

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "8197880482")

if not all([TG_TOKEN, HF_TOKEN, DASHSCOPE_API_KEY]):
    raise Exception("Не хватает токенов! Проверьте переменные на Railway.")

dashscope.base_http_api_url = 'https://dashscope-intl.aliyuncs.com/api/v1'

if os.environ.get("BOT_ACTIVE", "true").lower() != "true":
    sys.exit(0)

# --- БАЗА ДАННЫХ ---
DB_PATH = "/app/users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT UNIQUE,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        total_requests INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def log_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),))
    if c.fetchone():
        c.execute("UPDATE users SET username=?, first_name=?, last_name=?, last_seen=CURRENT_TIMESTAMP WHERE user_id=?", 
                  (username, first_name, last_name, str(user_id)))
    else:
        c.execute("INSERT INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)", 
                  (str(user_id), username, first_name, last_name))
    c.execute("INSERT INTO requests (user_id) VALUES (?)", (str(user_id),))
    c.execute("UPDATE users SET total_requests = total_requests + 1, last_seen = CURRENT_TIMESTAMP WHERE user_id = ?", 
              (str(user_id),))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM requests")
    total_requests = c.fetchone()[0]
    c.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 10")
    recent_users = c.fetchall()
    conn.close()
    return total_users, total_requests, recent_users

init_db()

# --- ТЕЛЕГРАМ БОТ ---
bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)
app = Flask(__name__)

# Временное хранилище для карточек пользователей
user_cards = {}

# --- ФУНКЦИИ ОБРАБОТКИ ИЗОБРАЖЕНИЙ ---

def retouch_photo(product_bytes):
    """Ретушь фото через Qwen"""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"
        prompt = "Удали лишние объекты с фотографии (руки, провода, блики от лампы). Оставь только сам товар. Помести его на нейтральный, чистый, студийный белый фон."
        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]
        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY, 
            model="qwen-image-edit-plus", 
            messages=messages, 
            n=1, 
            watermark=False, 
            size="1024*1536"
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
    except Exception as e:
        print(f"Ошибка ретуши: {e}")
        return None
    return None

def create_card(product_url):
    """Создание карточки товара"""
    try:
        prompt = "Создай премиальную карточку товара для Wildberries. Идеальный студийный фон, мягкий свет, товар в центре. НИКАКОГО текста."
        messages = [{"role": "user", "content": [{"image": product_url}, {"text": prompt}]}]
        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY, 
            model="qwen-image-2.0-pro", 
            messages=messages, 
            n=1, 
            watermark=False, 
            size="1024*1536"
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
    except Exception as e:
        print(f"Ошибка создания карточки: {e}")
        return None
    return None

def generate_product_description(image_url):
    """Генерация описания товара через AI"""
    try:
        prompt = "Проанализируй этот товар и создай для него короткое продающее название в 2-4 слова. Только название, без лишнего текста. На русском языке."
        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]
        
        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-vl-max",
            messages=messages
        )
        
        if response.status_code == 200:
            text = response.output.choices[0].message.content[0]['text']
            # Очистка от лишнего
            text = text.strip().replace('"', '').replace("'", "")
            return text[:50]  # Максимум 50 символов
    except Exception as e:
        print(f"Ошибка генерации описания: {e}")
        return None
    return None

def add_premium_text_to_image(image_url, title, subtitle=""):
    """
    Профессиональное наложение текста на изображение
    Дизайн в стиле премиум маркетплейсов
    """
    try:
        # Загрузка изображения
        if image_url.startswith('http'):
            response = requests.get(image_url)
            img = Image.open(BytesIO(response.content))
        elif image_url.startswith('data:image'):
            base64_data = image_url.split(',')[1]
            img_data = base64.b64decode(base64_data)
            img = Image.open(BytesIO(img_data))
        else:
            img = Image.open(image_url)
        
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        width, height = img.size
        
        # Создание слоя для рисования
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # --- НАСТРОЙКА ШРИФТОВ ---
        try:
            font_bold = ImageFont.truetype("/app/Font bold.ttf", int(height * 0.065))
            font_regular = ImageFont.truetype("/app/Font regular.ttf", int(height * 0.04))
        except:
            try:
                font_bold = ImageFont.truetype("Font bold.ttf", int(height * 0.065))
                font_regular = ImageFont.truetype("Font regular.ttf", int(height * 0.04))
            except:
                # Fallback на системный шрифт
                font_bold = ImageFont.load_default()
                font_regular = ImageFont.load_default()
        
        # --- ПОДГОТОВКА ТЕКСТА ---
        # Перенос длинного заголовка
        max_chars = 25
        if len(title) > max_chars:
            wrapped_title = textwrap.fill(title, width=max_chars)
        else:
            wrapped_title = title
        
        # --- РАСЧЕТ ПОЗИЦИЙ ---
        # Заголовок
        bbox_title = draw.multiline_textbbox((0, 0), wrapped_title, font=font_bold)
        title_width = bbox_title[2] - bbox_title[0]
        title_height = bbox_title[3] - bbox_title[1]
        
        # Позиция: верхняя часть, по центру
        title_x = (width - title_width) // 2
        title_y = int(height * 0.08)
        
        # Подзаголовок (если есть)
        subtitle_height = 0
        if subtitle:
            bbox_subtitle = draw.textbbox((0, 0), subtitle, font=font_regular)
            subtitle_width = bbox_subtitle[2] - bbox_subtitle[0]
            subtitle_height = bbox_subtitle[3] - bbox_subtitle[1]
            subtitle_x = (width - subtitle_width) // 2
            subtitle_y = title_y + title_height + 15
        
        # --- СОЗДАНИЕ ГРАДИЕНТНОЙ ПОДЛОЖКИ ---
        total_height = title_height + (subtitle_height + 15 if subtitle else 0) + 60
        
        # Градиент от темного к прозрачному
        gradient = Image.new('RGBA', (width, total_height), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        
        for i in range(total_height):
            alpha = int(160 * (1 - i / total_height))  # Градиент прозрачности
            gradient_draw.rectangle(
                [(0, i), (width, i + 1)],
                fill=(20, 20, 40, alpha)
            )
        
        # Наложение градиента
        overlay.paste(gradient, (0, title_y - 30), gradient)
        
        # --- РИСОВАНИЕ ДЕКОРАТИВНЫХ ЭЛЕМЕНТОВ ---
        # Акцентная линия над текстом
        line_width = int(title_width * 0.3)
        line_x = (width - line_width) // 2
        line_y = title_y - 15
        
        draw.rounded_rectangle(
            [line_x, line_y, line_x + line_width, line_y + 4],
            radius=2,
            fill=(255, 215, 0, 220)  # Золотой цвет
        )
        
        # --- РИСОВАНИЕ ТЕКСТА С ТЕНЬЮ ---
        shadow_offset = 3
        
        # Тень заголовка
        draw.multiline_text(
            (title_x + shadow_offset, title_y + shadow_offset),
            wrapped_title,
            font=font_bold,
            fill=(0, 0, 0, 140),
            align='center',
            spacing=8
        )
        
        # Основной заголовок (белый)
        draw.multiline_text(
            (title_x, title_y),
            wrapped_title,
            font=font_bold,
            fill=(255, 255, 255, 255),
            align='center',
            spacing=8
        )
        
        # Подзаголовок (если есть)
        if subtitle:
            # Тень подзаголовка
            draw.text(
                (subtitle_x + shadow_offset - 1, subtitle_y + shadow_offset - 1),
                subtitle,
                font=font_regular,
                fill=(0, 0, 0, 100)
            )
            
            # Основной подзаголовок (светло-серый)
            draw.text(
                (subtitle_x, subtitle_y),
                subtitle,
                font=font_regular,
                fill=(230, 230, 230, 255)
            )
        
        # --- ФИНАЛЬНОЕ ОБЪЕДИНЕНИЕ ---
        img = img.convert('RGBA')
        final_img = Image.alpha_composite(img, overlay)
        final_img = final_img.convert('RGB')
        
        # Легкое повышение резкости для четкости текста
        final_img = final_img.filter(ImageFilter.SHARPEN)
        
        # Сохранение в BytesIO
        output = BytesIO()
        final_img.save(output, format='JPEG', quality=95)
        output.seek(0)
        
        return output
        
    except Exception as e:
        print(f"Ошибка наложения текста: {e}")
        return None

# --- ОБРАБОТЧИКИ КОМАНД ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    welcome_text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 Отправьте фото товара — я:\n"
        "✨ Уберу лишние объекты\n"
        "🎨 Создам студийный фон\n"
        "📝 Добавлю красивый текст\n\n"
        "Просто отправьте фото!"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML")

@bot.message_handler(commands=['admin'])
def admin_stats(message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    
    total_users, total