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
    "clean_white": {"name": "🤍 Чистый белый", "prompt": "Clean pure white studio background, professional product photography, soft shadows", "brightness": 250, "text_color": (30, 30, 30), "accent_color": (100, 100, 100)},
    "gradient_warm": {"name": "🧡 Теплый градиент", "prompt": "Warm gradient background from peach to cream, soft lighting, premium feel", "brightness": 200, "text_color": (60, 40, 20), "accent_color": (180, 100, 60)},
    "dark_luxury": {"name": "🖤 Темная роскошь", "prompt": "Dark charcoal background, dramatic lighting, luxury premium product photography", "brightness": 50, "text_color": (255, 255, 255), "accent_color": (180, 180, 180)},
    "mint_fresh": {"name": "💚 Мятная свежесть", "prompt": "Soft mint green background, fresh clean look, organic natural product feel", "brightness": 220, "text_color": (20, 60, 40), "accent_color": (80, 160, 120)},
    "sky_blue": {"name": "💙 Небесный", "prompt": "Soft sky blue gradient background, airy light feel, tech modern product", "brightness": 210, "text_color": (20, 40, 80), "accent_color": (80, 140, 220)},
    "rose_gold": {"name": "🩷 Розовое золото", "prompt": "Rose gold pink background, feminine elegant, soft pink and gold tones", "brightness": 200, "text_color": (80, 30, 40), "accent_color": (200, 100, 120)},
    "neon_tech": {"name": "💜 Неоновый", "prompt": "Dark background with neon purple and blue accents, cyberpunk tech style, glowing edges", "brightness": 60, "text_color": (255, 255, 255), "accent_color": (200, 100, 255)},
    "wood_natural": {"name": "🤎 Натуральное дерево", "prompt": "Light wood texture background, natural organic feel, warm tones, eco friendly", "brightness": 180, "text_color": (60, 40, 20), "accent_color": (160, 120, 80)}
}

# --- ФУНКЦИИ ОБРАБОТКИ ИЗОБРАЖЕНИЙ ---
def retouch_photo(product_bytes, style_key="clean_white", angle_hint=""):
    """Создаёт готовую карточку с товаром на выбранном фоне."""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        
        angle_prompt = f"Show the product from a different angle. {angle_hint}. " if angle_hint else ""
        
        prompt = f"{angle_prompt}Place the product on a beautiful {style['prompt']}. Studio lighting, high quality, professional product photography. The image should look like a ready-made premium marketplace card WITHOUT ANY TEXT OR WATERMARKS."
        
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

# --- ШРИФТЫ ---
def get_font(size, weight='regular'):
    fonts = {
        'bold': ['/app/Montserrat-Bold.ttf', '/app/font_bold.ttf'],
        'medium': ['/app/Montserrat-Medium.ttf', '/app/font.ttf'],
        'regular': ['/app/Montserrat-Regular.ttf', '/app/font_regular.ttf']
    }
    font_list = fonts.get(weight, fonts['regular'])
    for font_path in font_list:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size)
                test_bbox = font.getbbox("ЙЦУКЕНГШЩЗ")
                if test_bbox and (test_bbox[2] - test_bbox[0]) > 50:
                    return font
            except:
                continue
    for fp in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()

# --- ИНФОГРАФИКА ---
def add_infographic(base_image, title, features=None, bonuses=None, triggers=None, style_key="clean_white"):
    """Накладывает стильный, читаемый текст на готовую карточку."""
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        width, height = base_image.size
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # --- НАСТРОЙКИ ЦВЕТА ---
        r, g, b = style['text_color']
        # Более прозрачный фон плашки
        plate_fill = (r, g, b, 40)  
        plate_outline = (r, g, b, 20)
        margin = int(width * 0.05)
        plate_radius = int(height * 0.02)
        
        # --- ЗАГОЛОВОК (НАЗВАНИЕ ТОВАРА) ---
        if title:
            title_text = title.upper()[:40]
            title_font = get_font(int(height * 0.035), 'bold')
            tw = draw.textbbox((0, 0), title_text, font=title_font)[2]
            # Заголовок размещается сверху, как и было
            draw.text(((width - tw) // 2, int(height * 0.03)), title_text, font=title_font, fill=style['text_color'])
        
        # --- ХАРАКТЕРИСТИКИ (ПЛАШКИ ПО БОКАМ) ---
        if features and len(features) > 0:
            # Фильтруем пустые или бессмысленные фичи
            valid_features = [f for f in features if f.get('value') and f['value'] != "НЕТ"]
            
            if valid_features:
                badge_w = int(width * 0.38)
                badge_h = int(height * 0.10)
                start_y = int(height * 0.22)
                gap = int(height * 0.02)
                for i, feat in enumerate(valid_features[:4]):
                    bx = margin if i % 2 == 0 else width - margin - badge_w
                    by = start_y + (i // 2) * (badge_h + gap)
                    
                    # Рисуем плашку
                    draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=plate_radius, fill=plate_fill, outline=plate_outline, width=1)
                    
                    value = feat.get('value', '')
                    label = feat.get('label', '')
                    
                    # --- ДИНАМИЧЕСКИЙ ПОДБОР ШРИФТА, ЧТОБЫ ТЕКСТ НЕ ВЫЛЕЗАЛ ---
                    max_val_width = badge_w - 20
                    val_font_size = int(height * 0.03)
                    val_font = get_font(val_font_size, 'bold')
                    
                    # Уменьшаем шрифт, пока текст не влезет в плашку
                    while draw.textbbox((0, 0), value, font=val_font)[2] > max_val_width and val_font_size > 10:
                        val_font_size -= 1
                        val_font = get_font(val_font_size, 'bold')
                    
                    # Переносим слово, если оно всё ещё слишком длинное
                    if draw.textbbox((0, 0), value, font=val_font)[2] > max_val_width:
                        display_value = value[:15] + ".."
                    else:
                        display_value = value

                    # Рисуем текст на плашке
                    draw.text((bx + 10, by + int(height * 0.02)), display_value, font=val_font, fill=style['text_color'])
                    
                    # Лейбл рисуем только если он валидный
                    if label and label != "НЕТ" and "Особенность" not in label:
                        label_font = get_font(int(height * 0.02), 'regular')
                        draw.text((bx + 10, by + int(height * 0.06)), label, font=label_font, fill=(r, g, b, 180))
        
        # --- БОНУСЫ (НИЖНИЕ ПЛАШКИ) ---
        y_bonus = int(height * 0.68)
        if bonuses and len(bonuses) > 0:
            for bonus in bonuses[:2]:
                text = bonus[:35]
                # Фильтруем мусорные бонусы
                if not text or text == "НЕТ": continue
                    
                bonus_font = get_font(int(height * 0.03), 'medium')
                tw = draw.textbbox((0, 0), text, font=bonus_font)[2] + 30
                bh = int(height * 0.06)
                draw.rounded_rectangle([((width - tw) // 2, y_bonus), ((width - tw) // 2 + tw, y_bonus + bh)], radius=plate_radius, fill=(r, g, b, 40), outline=(r, g, b, 80), width=1)
                draw.text(((width - tw) // 2 + 15, y_bonus + 8), text, font=bonus_font, fill=style['text_color'])
                y_bonus += bh + 10
        
        # --- ТРИГГЕРЫ (ПЛАШКИ С АКЦИЯМИ) ---
        if triggers and len(triggers) > 0:
            for trigger in triggers[:2]:
                text = trigger[:35]
                if not text or text == "НЕТ": continue
                    
                trigger_font = get_font(int(height * 0.025), 'medium')
                tw = draw.textbbox((0, 0), text, font=trigger_font)[2] + 30
                th = int(height * 0.05)
                draw.rounded_rectangle([((width - tw) // 2, y_bonus), ((width - tw) // 2 + tw, y_bonus + th)], radius=plate_radius, fill=(r, g, b, 50), outline=(r, g, b, 100), width=1)
                draw.text(((width - tw) // 2 + 15, y_bonus + 6), text, font=trigger_font, fill=style['text_color'])
                y_bonus += th + 8
        
        # --- СБОРКА ---
        final = Image.alpha_composite(base_image, overlay)
        final = final.convert('RGB')
        enhancer = ImageEnhance.Contrast(final)
        final = enhancer.enhance(1.1)
        output = BytesIO()
        final.save(output, format='JPEG', quality=95)
        output.seek(0)
        return output
    except Exception as e:
        print(f"❌ Ошибка инфографики: {e}")
    return None

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
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for key, style in BG_STYLES.items():
        markup.add(telebot.types.InlineKeyboardButton(style['name'], callback_data=f"style_{key}"))
    msg = bot.send_message(message.chat.id, "🎨 <b>Выберите стиль фона:</b>", parse_mode="HTML", reply_markup=markup)
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
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("🤖 AI Вопросы", callback_data="mode_ai"),
            telebot.types.InlineKeyboardButton("✨ Авто-генерация", callback_data="mode_auto"),
            telebot.types.InlineKeyboardButton("🛠️ Конструктор", callback_data="mode_manual")
        )
        bot.edit_message_text("🎯 <b>Выберите режим работы:</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
    elif data == "mode_ai":
        bot.answer_callback_query(call.id)
        run_ai_mode(call.message, user_id)

def run_ai_mode(message, user_id):
    chat_id = message.chat.id
    status_msg = bot.send_message(chat_id, "🔎 <b>Начинаю анализ товара...</b>")
    progress_analysis(chat_id, status_msg.message_id, user_id)

def progress_analysis(chat_id, msg_id, user_id):
    photo = user_data[user_id]['photo']
    style_key = user_data[user_id]['style']
    
    # Шаг 1: Готовая карточка с фронтального ракурса
    bot.edit_message_text("🔎 <b>Анализ: Шаг 1/4 — Создаю карточку</b> (⏱️ ~15 сек)", chat_id, msg_id, parse_mode="HTML")
    base_card_url = retouch_photo(photo, style_key)
    if not base_card_url:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось обработать фото.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Шаг 2: Создаём дополнительные ракурсы
    bot.edit_message_text("🔎 <b>Анализ: Шаг 2/4 — Создаю ракурсы</b> (⏱️ ~20 сек)", chat_id, msg_id, parse_mode="HTML")
    left_card_url = retouch_photo(photo, style_key, "angle slightly from the left side, 3/4 view")
    right_card_url = retouch_photo(photo, style_key, "angle slightly from the right side, 3/4 view")
    
    # Шаг 3: Глубокий AI-анализ
    bot.edit_message_text("🧠 <b>Анализ: Шаг 3/4 — Изучаю товар</b> (⏱️ ~25 сек)", chat_id, msg_id, parse_mode="HTML")
    analysis = deep_analyze_and_generate_questions(base_card_url)
    if not analysis:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось проанализировать товар.", chat_id, msg_id, parse_mode="HTML")
        return
    
    # Сохраняем результаты
    user_data[user_id]['base_card'] = base_card_url
    user_data[user_id]['left_card'] = left_card_url
    user_data[user_id]['right_card'] = right_card_url
    user_analysis[user_id] = analysis
    user_analysis[user_id]['answers'] = []
    user_analysis[user_id]['current_q'] = 0
    
    questions = analysis.get('questions', [])
    num_questions = len(questions)
    
    bot.edit_message_text(
        f"✅ <b>Анализ завершён!</b>\n\n📦 <b>Категория:</b> {analysis.get('category', 'Товар')}\n🎯 <b>Целевая аудитория:</b> {analysis.get('target_audience', 'Не определена')}\n❓ <b>Подготовлено вопросов:</b> {num_questions}\n\n<i>Начинаю опрос...</i>",
        chat_id, msg_id, parse_mode="HTML"
    )
    ask_next_question(chat_id, user_id)

def deep_analyze_and_generate_questions(image_url):
    prompt = """You are an expert e-commerce strategist. Analyze the product in the image and provide a detailed JSON response.
RESPOND ONLY WITH VALID JSON.
{
  "category": "string, product category in Russian",
  "target_audience": "string, target audience in Russian",
  "key_features": ["string, main feature 1", "..."],
  "questions": [
    "string, unique question 1 in Russian",
    "string, unique question 2 in Russian",
    "... at least 5 questions, more if product is complex"
  ]
}
Questions should help the seller create a compelling marketplace card.
They MUST be UNIQUE for this specific product category.
Focused on MATERIALS, COMPATIBILITY, BONUSES, PROMOTIONS, COMPETITIVE ADVANTAGES.
At least 5 questions long. For complex products (like electronics), generate 7-10 questions.
"""
    try:
        if image_url.startswith('http'):
            base64_image = base64.b64encode(requests.get(image_url).content).decode('utf-8')
        else:
            base64_image = None
        if not base64_image: return None
        messages = [{"role": "user", "content": [{"image": f"data:image/jpeg;base64,{base64_image}"}, {"text": prompt}]}]
        response = MultiModalConversation.call(api_key=DASHSCOPE_API_KEY, model="qwen-vl-max", messages=messages)
        if response.status_code == 200:
            text = response.output.choices[0].message.content[0]['text']
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        print(f"Ошибка глубокого анализа: {e}")
    return None

def ask_next_question(chat_id, user_id):
    analysis = user_analysis.get(user_id, {})
    questions = analysis.get('questions', [])
    current_q = analysis.get('current_q', 0)
    if current_q < len(questions):
        question = questions[current_q]
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("⏭️ Пропустить вопрос", callback_data="skip_question"))
        bot.send_message(chat_id, f"🤖 <b>Вопрос {current_q + 1} из {len(questions)}</b>\n\n{question}", parse_mode="HTML", reply_markup=markup)
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
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    # --- !!! НОВОЕ: получаем список вопросов, чтобы сделать из них метки !!! ---
    questions = analysis.get('questions', [])
    style_key = user_data[user_id]['style']
    title = analysis.get('product_name', 'ПРЕМИУМ ТОВАР').upper()

    features = []
    bonuses = []
    triggers = []

    # --- ПРЕВРАЩАЕМ ВОПРОСЫ В МЕТКИ ДЛЯ ПЛАШЕК ---
    def question_to_label(question):
        """Преобразует вопрос в короткую характеристику на русском"""
        if not question: return "Характеристика"
        q = question.lower()
        if 'материал' in q: return 'Материал'
        if 'модел' in q or 'совместим' in q or 'устройств' in q: return 'Совместимость'
        if 'бонус' in q or 'подар' in q or 'бесплат' in q: return 'Бонус'
        if 'скидк' in q or 'акци' in q or 'промокод' in q: return 'Акция'
        if 'лучше' in q or 'конкурент' in q or 'преимуществ' in q: return 'Преимущество'
        if 'гаранти' in q or 'качеств' in q: return 'Гарантия'
        if 'размер' in q or 'габарит' in q: return 'Размер'
        if 'цвет' in q: return 'Цвет'
        # Если вопрос не опознан, берём первые 2-3 слова
        words = question.split()[:3]
        return ' '.join(words) if words else 'Характеристика'

    for i, ans in enumerate(answers):
        if not ans: continue
        # Берём вопрос, который был задан, чтобы сделать из него метку
        question = questions[i] if i < len(questions) else ""
        label = question_to_label(question)

        if i == 0 and len(ans) > 2: features.append({"icon": "🔷", "label": label, "value": ans[:20]})
        elif i == 1 and len(ans) > 2: features.append({"icon": "✅", "label": label, "value": ans[:20]})
        elif i == 2 and len(ans) > 2: features.append({"icon": "⭐", "label": label, "value": ans[:20]})
        elif i == 3 and len(ans) > 2: bonuses.append(f"🎁 {ans[:25]}")
        elif i == 4 and len(ans) > 2: triggers.append(f"⏰ {ans[:25]}")
        elif len(ans) > 2: features.append({"icon": "📦", "label": label, "value": ans[:20]})

    if not bonuses: bonuses.append("🚚 Быстрая доставка")
    if not triggers: triggers.append("🔥 Хит продаж")

    # --- ГЕНЕРАЦИЯ ТРЁХ КАРТОЧЕК (без изменений) ---
    cards = []
    base_img = Image.open(BytesIO(requests.get(user_data[user_id]['base_card']).content)).convert('RGBA')
    left_img = Image.open(BytesIO(requests.get(user_data[user_id].get('left_card', user_data[user_id]['base_card'])).content)).convert('RGBA')
    right_img = Image.open(BytesIO(requests.get(user_data[user_id].get('right_card', user_data[user_id]['base_card'])).content)).convert('RGBA')

    card1 = add_infographic(base_img, title, features[:1] if features else None, None, None, style_key)
    if card1: cards.append(card1)
    card2 = add_infographic(left_img, None, features[1:3] if len(features) > 1 else None, None, None, style_key)
    if card2: cards.append(card2)
    card3 = add_infographic(right_img, None, features[3:4] if len(features) > 3 else None, bonuses, triggers, style_key)
    if card3: cards.append(card3)

    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/3")
    else:
        bot.send_message(chat_id, "❌ Ошибка при создании карточек.")

# === ЗАПУСК ===
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return '', 403
    
if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"✅ Webhook установлен: {railway_url}")
    print("🚀 Бот запущен!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
