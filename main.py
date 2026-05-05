import os
import sys
import json
import base64
import sqlite3
import re
from datetime import datetime
from io import BytesIO
import textwrap
import glob

import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import dashscope
from dashscope import MultiModalConversation
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import requests

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "7101357158")

if not all([TG_TOKEN, HF_TOKEN, DASHSCOPE_API_KEY]):
    raise Exception("Не хватает токенов! Проверьте переменные на Railway.")

dashscope.base_http_api_url = 'https://dashscope-intl.aliyuncs.com/api/v1'

if os.environ.get("BOT_ACTIVE", "true").lower() != "true":
    sys.exit(0)

# --- БАЗА ДАННЫХ (без изменений) ---
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

def set_bot_commands():
    commands = [
        telebot.types.BotCommand("start", "🚀 Запустить бота"),
        telebot.types.BotCommand("admin", "📊 Админ-панель"),
    ]
    try:
        bot.set_my_commands(commands)
        print("Команды меню установлены")
    except Exception as e:
        print(f"Не удалось установить команды: {e}")

set_bot_commands()

# Временное хранилище
user_data = {}
user_analysis = {}

# --- СТИЛИ ФОНОВ ---
BG_STYLES = {
    "clean_white": {"name": "🤍 Чистый белый", "prompt": "Clean pure white studio background, professional product photography, soft shadows", "brightness": 250, "text_color": (30, 30, 30), "accent_color": (100, 100, 100)""},
    "gradient_warm": {"name": "🧡 Теплый градиент", "prompt": "Warm gradient background from peach to cream, soft lighting, premium feel", "brightness": 200, "text_color": (60, 40, 20), "accent_color": (180, 100, 60)""},
    "dark_luxury": {"name": "🖤 Темная роскошь", "prompt": "Dark charcoal background, dramatic lighting, luxury premium product photography", "brightness": 50, "text_color": (255, 255, 255), "accent_color": (180, 180, 180)""},
    "mint_fresh": {"name": "💚 Мятная свежесть", "prompt": "Soft mint green background, fresh clean look, organic natural product feel", "brightness": 220, "text_color": (20, 60, 40), "accent_color": (80, 160, 120)""},
    "sky_blue": {"name": "💙 Небесный", "prompt": "Soft sky blue gradient background, airy light feel, tech modern product", "brightness": 210, "text_color": (20, 40, 80), "accent_color": (80, 140, 220)""},
    "rose_gold": {"name": "🩷 Розовое золото", "prompt": "Rose gold pink background, feminine elegant, soft pink and gold tones", "brightness": 200, "text_color": (80, 30, 40), "accent_color": (200, 100, 120)""},
    "neon_tech": {"name": "💜 Неоновый", "prompt": "Dark background with neon purple and blue accents, cyberpunk tech style, glowing edges", "brightness": 60, "text_color": (255, 255, 255), "accent_color": (200, 100, 255)""},
    "wood_natural": {"name": "🤎 Натуральное дерево", "prompt": "Light wood texture background, natural organic feel, warm tones, eco friendly", "brightness": 180, "text_color": (60, 40, 20), "accent_color": (160, 120, 80)"}
}

# --- ФУНКЦИИ ОБРАБОТКИ ИЗОБРАЖЕНИЙ (Оставлены без изменений для краткости, но они должны быть здесь) ---
# Вставьте сюда все функции обработки изображений из предыдущего ответа:
# isolate_product, generate_background, get_font, compose_card, add_infographic

def analyze_product_and_generate_questions(image_url):
    """
    Глубокий анализ товара с помощью AI.
    Возвращает словарь с категорией, характеристиками и СПИСКОМ УНИКАЛЬНЫХ вопросов.
    """
    # Эта функция будет заменена на новую, улучшенную.
    pass

# === ОБРАБОТЧИКИ ===
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    welcome_text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 <b>Просто отправьте мне фото товара!</b>"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    # 1. Выбор стиля
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for key, style in BG_STYLES.items():
        markup.add(telebot.types.InlineKeyboardButton(style['name'], callback_data=f"style_{key}"))
    
    msg = bot.send_message(
        message.chat.id,
        "🎨 <b>Выберите стиль фона:</b>",
        parse_mode="HTML",
        reply_markup=markup
    )
    
    # Сохраняем фото
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    user_data[user_id] = {'photo': downloaded_file, 'style': None}
    user_analysis[user_id] = {}

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = str(call.from_user.id)
    data = call.data
    
    if data.startswith("style_"):
        style_key = data.replace("style_", "")
        user_data[user_id]['style'] = style_key
        bot.answer_callback_query(call.id, "✅ Стиль выбран")
        
        # Предлагаем режимы работы
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("🤖 AI Вопросы", callback_data="mode_ai"),
            telebot.types.InlineKeyboardButton("✨ Авто-генерация", callback_data="mode_auto"),
            telebot.types.InlineKeyboardButton("🛠️ Конструктор", callback_data="mode_manual")
        )
        bot.edit_message_text(
            "🎯 <b>Выберите режим работы:</b>",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=markup
        )
    
    elif data == "mode_ai":
        bot.answer_callback_query(call.id)
        run_ai_mode(call.message, user_id)
        
    # ... (остальные callback'и)

def run_ai_mode(message, user_id):
    """Запускает умный анализ и генерацию вопросов."""
    chat_id = message.chat.id
    
    # Сообщение о начале анализа
    status_msg = bot.send_message(chat_id, "🔎 <b>Начинаю анализ товара...</b>")
    
    # Выполняем анализ с детальным описанием этапов
    progress_analysis(chat_id, status_msg.message_id, user_id)

def progress_analysis(chat_id, msg_id, user_id):
    """Поэтапно обновляет статус анализа."""
    
    photo = user_data[user_id]['photo']
    style_key = user_data[user_id]['style']
    
    # Этап 1: Изоляция
    bot.edit_message_text("🔎 <b>Анализ: Шаг 1/4 — Выделяю товар</b> (⏱️ ~10 сек)", chat_id, msg_id, parse_mode="HTML")
    isolated_url = isolate_product(photo)
    if not isolated_url:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось обработать фото.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Этап 2: Фон
    bot.edit_message_text("🔎 <b>Анализ: Шаг 2/4 — Создаю фон</b> (⏱️ ~15 сек)", chat_id, msg_id, parse_mode="HTML")
    bg_url = generate_background(style_key)
    if not bg_url:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось создать фон.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Этап 3: Сборка
    bot.edit_message_text("🔎 <b>Анализ: Шаг 3/4 — Собираю карточку</b> (⏱️ ~5 сек)", chat_id, msg_id, parse_mode="HTML")
    base_card = compose_card(isolated_url, bg_url)
    if not base_card:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось собрать карточку.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Этап 4: Глубокий AI-анализ и генерация вопросов
    bot.edit_message_text("🧠 <b>Анализ: Шаг 4/4 — Изучаю товар и придумываю вопросы</b> (⏱️ ~25 сек)", chat_id, msg_id, parse_mode="HTML")
    analysis = deep_analyze_and_generate_questions(isolated_url)
    
    if not analysis:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось проанализировать товар.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Сохраняем результаты
    user_data[user_id]['isolated'] = isolated_url
    user_data[user_id]['base_card'] = base_card
    user_analysis[user_id] = analysis
    user_analysis[user_id]['answers'] = []
    user_analysis[user_id]['current_q'] = 0
    
    questions = analysis.get('questions', [])
    num_questions = len(questions)
    
    # Завершающее сообщение
    bot.edit_message_text(
        f"✅ <b>Анализ завершён!</b>\n\n"
        f"📦 <b>Категория:</b> {analysis.get('category', 'Товар')}\n"
        f"🎯 <b>Целевая аудитория:</b> {analysis.get('target_audience', 'Не определена')}\n"
        f"❓ <b>Подготовлено вопросов:</b> {num_questions}\n\n"
        f"<i>Начинаю опрос...</i>",
        chat_id, msg_id, parse_mode="HTML"
    )
    
    # Запускаем первый вопрос
    ask_next_question(chat_id, user_id)

def deep_analyze_and_generate_questions(image_url):
    """
    Глубокий анализ товара и генерация уникальных вопросов.
    """
    prompt = """You are an expert e-commerce strategist. Analyze the product in the image and provide a detailed JSON response.

RESPOND ONLY WITH VALID JSON.

{
  "category": "string, product category in Russian (e.g., 'Наушники')",
  "target_audience": "string, target audience in Russian (e.g., 'Молодежь, геймеры')",
  "key_features": ["string, main feature 1", "..."],
  "questions": [
    "string, unique question 1 in Russian",
    "string, unique question 2 in Russian",
    "... at least 5 questions, more if product is complex"
  ]
}

Questions should help the seller create a compelling marketplace card.
They MUST be:
- UNIQUE for this specific product category.
- Focused on MATERIALS, COMPATIBILITY, BONUSES, PROMOTIONS, COMPETITIVE ADVANTAGES.
- At least 5 questions long. For complex products (like electronics), generate 7-10 questions.
"""
    
    try:
        base64_image = base64.b64encode(requests.get(image_url).content).decode('utf-8') if image_url.startswith('http') else None
        if not base64_image: return None
        
        messages = [{"role": "user", "content": [{"image": f"data:image/jpeg;base64,{base64_image}"}, {"text": prompt}]}]
        response = MultiModalConversation.call(api_key=DASHSCOPE_API_KEY, model="qwen-vl-max", messages=messages)
        
        if response.status_code == 200:
            text = response.output.choices[0].message.content[0]['text']
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        print(f"Deep analysis error: {e}")
    return None

def ask_next_question(chat_id, user_id):
    """Задаёт следующий вопрос пользователю."""
    analysis = user_analysis.get(user_id, {})
    questions = analysis.get('questions', [])
    current_q = analysis.get('current_q', 0)
    
    if current_q < len(questions):
        question = questions[current_q]
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("⏭️ Пропустить вопрос", callback_data="skip_question"))
        
        bot.send_message(
            chat_id,
            f"🤖 <b>Вопрос {current_q + 1} из {len(questions)}</b>\n\n{question}",
            parse_mode="HTML",
            reply_markup=markup
        )
    else:
        finish_ai_mode(chat_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data == "skip_question")
def skip_question_callback(call):
    user_id = str(call.from_user.id)
    analysis = user_analysis.get(user_id)
    if analysis:
        analysis['answers'].append("")
        analysis['current_q'] += 1
        bot.answer_callback_query(call.id, "⏭️ Пропущен")
        ask_next_question(call.message.chat.id, user_id)

@bot.message_handler(func=lambda m: True)
def handle_answer(message):
    user_id = str(message.from_user.id)
    analysis = user_analysis.get(user_id)
    if analysis and analysis.get('current_q') is not None:
        analysis['answers'].append(message.text.strip())
        analysis['current_q'] += 1
        ask_next_question(message.chat.id, user_id)
    else:
        bot.send_message(message.chat.id, "Пожалуйста, начните с отправки фото.")

def finish_ai_mode(chat_id, user_id):
    """Завершает диалог и генерирует итоговые карточки."""
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    base_card = user_data[user_id]['base_card']
    style_key = user_data[user_id]['style']
    
    features = []
    bonuses = []
    triggers = []
    title = analysis.get('product_name', 'ПРЕМИУМ ТОВАР').upper()

    for i, ans in enumerate(answers):
        if not ans: continue
        if i == 0 and len(ans) > 2: features.append({"icon": "🔷", "label": "Материал", "value": ans[:20]})
        elif i == 1 and len(ans) > 2: features.append({"icon": "✅", "label": "Совместимость", "value": ans[:20]})
        elif i == 2 and len(ans) > 2: bonuses.append(f"🎁 {ans[:25]}")
        elif i == 3 and len(ans) > 2: triggers.append(f"⏰ {ans[:25]}")
        elif len(ans) > 2: features.append({"icon": "⭐", "label": analysis.get('key_features', [""])[0][:20] if analysis.get('key_features') else "Характеристика", "value": ans[:20]})

    if len(features) < 2: features.append({"icon": "📦", "label": "Категория", "value": analysis.get('category', 'Товар')[:20]})
    if not bonuses: bonuses.append("🚚 Быстрая доставка")
    if not triggers: triggers.append("🔥 Хит продаж")

    cards = []
    card1 = add_infographic(base_card.copy(), title, features[:2], None, None, style_key)
    if card1: cards.append(card1)
    card2 = add_infographic(base_card.copy(), None, features, None, None, style_key)
    if card2: cards.append(card2)
    card3 = add_infographic(base_card.copy(), None, None, bonuses, triggers, style_key)
    if card3: cards.append(card3)

    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/3")
    else:
        bot.send_message(chat_id, "❌ Ошибка при создании карточек.")

# === ЗАПУСК ===
if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"✅ Webhook установлен: {railway_url}")
    print("🚀 Бот запущен!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
