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
    "clean_white": {
        "name": "🤍 Чистый белый",
        "prompt": "Clean pure white studio background, professional product photography, soft shadows",
        "bg_color": (248, 248, 248),
        "text_color": (50, 50, 50),
        "accent_color": (0, 120, 200),
    },
    "gradient_warm": {
        "name": "🧡 Теплый градиент",
        "prompt": "Warm gradient background from peach to cream, soft lighting, premium feel",
        "bg_color": (253, 240, 230),
        "text_color": (80, 45, 30),
        "accent_color": (220, 100, 50),
    },
    "dark_luxury": {
        "name": "🖤 Темная роскошь",
        "prompt": "Dark charcoal background, dramatic lighting, luxury premium product photography",
        "bg_color": (34, 34, 38),
        "text_color": (240, 240, 245),
        "accent_color": (255, 215, 0),
    },
    "mint_fresh": {
        "name": "💚 Мятная свежесть",
        "prompt": "Soft mint green background, fresh clean look, organic natural product feel",
        "bg_color": (235, 250, 245),
        "text_color": (25, 60, 45),
        "accent_color": (0, 160, 130),
    },
    "sky_blue": {
        "name": "💙 Небесный",
        "prompt": "Soft sky blue gradient background, airy light feel, tech modern product",
        "bg_color": (235, 245, 255),
        "text_color": (25, 50, 80),
        "accent_color": (0, 100, 210),
    },
    "rose_gold": {
        "name": "🩷 Розовое золото",
        "prompt": "Rose gold pink background, feminine elegant, soft pink and gold tones",
        "bg_color": (252, 240, 245),
        "text_color": (90, 40, 50),
        "accent_color": (210, 80, 110),
    },
    "neon_tech": {
        "name": "💜 Неоновый",
        "prompt": "Dark background with neon purple and blue accents, cyberpunk tech style, glowing edges",
        "bg_color": (25, 22, 40),
        "text_color": (245, 240, 255),
        "accent_color": (180, 130, 255),
    },
    "wood_natural": {
        "name": "🤎 Натуральное дерево",
        "prompt": "Light wood texture background, natural organic feel, warm tones, eco friendly",
        "bg_color": (245, 240, 230),
        "text_color": (70, 50, 30),
        "accent_color": (150, 100, 60),
    }
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
        'bold': ['/app/Montserrat-Bold.ttf', '/app/Montserrat-Black.ttf'],
        'medium': ['/app/Montserrat-Medium.ttf'],
        'regular': ['/app/Montserrat-Regular.ttf']
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
def add_infographic(base_image, title, features=None, style_key="clean_white"):
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        width, height = base_image.size
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        r, g, b = style['text_color']
        plate_fill = (r, g, b, 8)  # Минимальная прозрачность
        plate_outline = (r, g, b, 5)  # Почти невидимая обводка
        text_fill = (r, g, b, 240)
        margin = int(width * 0.05)
        plate_radius = int(height * 0.02)
        
        if title:
            title_text = title.upper()[:40]
            title_font = get_font(int(height * 0.06), 'bold')  # Массивный заголовок
            tw = draw.textbbox((0, 0), title_text, font=title_font)[2]
            draw.text(((width - tw) // 2, int(height * 0.03)), title_text, font=title_font, fill=text_fill)
        
        if features and len(features) > 0:
            badge_w = int(width * 0.85)
            badge_h = int(height * 0.14)
            start_y = int(height * 0.28)
            gap = int(height * 0.04)
            
            for i, feat in enumerate(features[:3]):
                bx = (width - badge_w) // 2
                by = start_y + i * (badge_h + gap)
                
                draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=plate_radius, fill=plate_fill, outline=plate_outline, width=1)
                
                text = feat.get('text', '')
                if text:
                    font_size = int(height * 0.04)
                    font = get_font(font_size, 'regular')
                    while draw.textbbox((0, 0), text, font=font)[2] > badge_w - 40 and font_size > 16:
                        font_size -= 2
                        font = get_font(font_size, 'regular')
                    tw = draw.textbbox((0, 0), text, font=font)[2]
                    tx = bx + (badge_w - tw) // 2
                    ty = by + (badge_h - font_size) // 2
                    draw.text((tx, ty), text, font=font, fill=text_fill)
        
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

# === GPT-2 МОДУЛЬ ===
def generate_description_gpt2(product_data, question, answer):
    prompt = f"Product: {product_data.get('category', 'товар')}. Seller question: '{question}'. Seller answer: '{answer}'. Create a short product feature in Russian (3-6 words) in format 'Label: Value'. Examples: 'Материал: Натуральная кожа', 'Совместимость: Все модели Ray-Ban', 'Комплектация: Салфетка в подарок'. Your response:"
    
    try:
        response = hf_client.text_generation(
            model="gpt2-medium",
            prompt=prompt,
            max_new_tokens=20,
            temperature=0.7
        )
        generated = response[0]['generated_text'].replace(prompt, '').strip()
        generated = generated.split('\n')[0]
        generated = generated.replace('"', '').replace("'", "").strip()
        return generated[:40]
    except Exception as e:
        print(f"Ошибка GPT-2: {e}")
        return f"{answer[:25]}"

def clean_answer(answer, question):
    ans = answer.strip()
    q = question.lower()
    
    if ans.lower() in ["нет", "no", "нету", "отсутствует", "бонусов нет"]:
        return None
    if 'нет' in ans.lower() and len(ans) < 10:
        return None
    
    if 'акци' in q or 'скидк' in q:
        if ans.lower().startswith('да'):
            return ans.replace('Да,', 'Скидка').replace('да,', 'Скидка')
    
    if ('совместим' in q or 'подходит' in q) and ans.lower() == 'да':
        return 'Совместимо'
    
    if ans.lower() == 'да':
        return None
    
    return ans

def sort_features_by_priority(features):
    priority_order = {
        'материал': 0, 'размер': 0, 'цвет': 0, 'модель': 0,
        'совместимость': 0, 'комплект': 0, 'гарантия': 1,
        'бонус': 2, 'подарок': 2, 'акция': 2, 'скидка': 2, 'доставка': 2,
    }
    
    def get_priority(feature):
        text = feature.get('text', '').lower()
        for key, priority in priority_order.items():
            if key in text:
                return priority
        return 3
    
    features.sort(key=get_priority)
    return features

# === ОБРАБОТЧИКИ GPT-2 РЕДАКТИРОВАНИЯ ===
def finish_ai_mode(chat_id, user_id):
    msg = bot.send_message(chat_id, "✏️ Введите заголовок карточки (название товара):")
    bot.register_next_step_handler(msg, process_title_step, chat_id, user_id)

def process_title_step(message, chat_id, user_id):
    title = message.text.strip().upper()[:40] if message.text.strip() else "ТОВАР"
    bot.send_message(chat_id, "🧠 Обрабатываю ваши ответы...")
    generate_gpt2_texts(chat_id, user_id, title)

def generate_gpt2_texts(chat_id, user_id, title):
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    questions = analysis.get('questions', [])
    
    clean_pairs = []
    for i, ans in enumerate(answers):
        if not ans: 
            continue
        
        if ans.strip().lower() in ["нет", "no", "нету", "отсутствует", "бонусов нет"]:
            continue
        if 'нет' in ans.lower() and len(ans) < 10:
            continue
            
        question = questions[i] if i < len(questions) else ""
        clean_ans = clean_answer(ans, question)
        if not clean_ans or len(clean_ans) <= 1:
            continue
            
        clean_pairs.append((question, clean_ans))
    
    if not clean_pairs:
        bot.send_message(chat_id, "❌ Недостаточно данных для карточек")
        return
    
    gpt2_results = []
    for question, answer in clean_pairs:
        generated = generate_description_gpt2(analysis, question, answer)
        gpt2_results.append({
            "question": question,
            "answer": answer,
            "generated": generated
        })
    
    user_analysis[user_id]['gpt2_results'] = gpt2_results
    user_analysis[user_id]['title'] = title
    user_analysis[user_id]['edit_index'] = 0
    
    show_gpt2_result(chat_id, user_id, 0)

def show_gpt2_result(chat_id, user_id, index):
    gpt2_results = user_analysis[user_id].get('gpt2_results', [])
    
    if index >= len(gpt2_results):
        generate_final_cards(chat_id, user_id)
        return
    
    item = gpt2_results[index]
    
    text = f"📝 <b>Вопрос:</b> {item['question']}\n💬 <b>Ваш ответ:</b> {item['answer']}\n✨ <b>Предлагаемый текст:</b> {item['generated']}\n\nОставить или изменить?"
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Оставить", callback_data=f"gpt2_accept_{index}"),
        telebot.types.InlineKeyboardButton("✏️ Изменить", callback_data=f"gpt2_edit_{index}")
    )
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("gpt2_"))
def handle_gpt2_callback(call):
    user_id = str(call.from_user.id)
    data = call.data
    
    if data.startswith("gpt2_accept_"):
        index = int(data.replace("gpt2_accept_", ""))
        bot.answer_callback_query(call.id, "✅ Принято")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        user_analysis[user_id]['edit_index'] = index + 1
        show_gpt2_result(call.message.chat.id, user_id, index + 1)
    
    elif data.startswith("gpt2_edit_"):
        index = int(data.replace("gpt2_edit_", ""))
        bot.answer_callback_query(call.id, "✏️ Введите новый текст")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        user_analysis[user_id]['edit_index'] = index
        
        msg = bot.send_message(call.message.chat.id, "✏️ Введите новый текст для этой плашки:")
        bot.register_next_step_handler(msg, process_edit_gpt2, call.message.chat.id, user_id)

def process_edit_gpt2(message, chat_id, user_id):
    new_text = message.text.strip()[:40]
    index = user_analysis[user_id].get('edit_index', 0)
    
    user_analysis[user_id]['gpt2_results'][index]['generated'] = new_text
    
    user_analysis[user_id]['edit_index'] = index + 1
    show_gpt2_result(chat_id, user_id, index + 1)

def generate_final_cards(chat_id, user_id):
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    
    analysis = user_analysis.get(user_id, {})
    gpt2_results = analysis.get('gpt2_results', [])
    title = analysis.get('title', 'ТОВАР')
    style_key = user_data[user_id]['style']
    
    all_features = [{"text": item['generated'][:40]} for item in gpt2_results]
    all_features = sort_features_by_priority(all_features)
    
    if not all_features:
        bot.send_message(chat_id, "❌ Недостаточно данных для карточек")
        return
    
    image_urls = [
        user_data[user_id].get('base_card'),
        user_data[user_id].get('left_card', user_data[user_id].get('base_card')),
        user_data[user_id].get('right_card', user_data[user_id].get('base_card')),
        user_data[user_id].get('far_left_card', user_data[user_id].get('base_card')),
        user_data[user_id].get('far_right_card', user_data[user_id].get('base_card')),
    ]
    
    cards = []
    for i, feature in enumerate(all_features):
        img_url = image_urls[i % len(image_urls)]
        if not img_url: continue
        
        try:
            img = Image.open(BytesIO(requests.get(img_url).content)).convert('RGBA')
        except:
            continue
        
        card = add_infographic(img, title, [feature], style_key)
        if card:
            cards.append(card)
    
    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/{len(cards)}")
    else:
        bot.send_message(chat_id, "❌ Ошибка при создании карточек.")

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
    
    if data.startswith("gpt2_"):
        return
    
    if data.startswith("style_"):
        style_key = data.replace("style_", "")
        user_data[user_id]['style'] = style_key
        bot.answer_callback_query(call.id, "✅ Стиль выбран")
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("🤖 Вопросы", callback_data="mode_ai"),
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
    
    bot.edit_message_text("🔎 <b>Анализ: Шаг 1/4 — Создаю карточку</b> (⏱️ ~15 сек)", chat_id, msg_id, parse_mode="HTML")
    base_card_url = retouch_photo(photo, style_key)
    if not base_card_url:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось обработать фото.", chat_id, msg_id, parse_mode="HTML")
        return
    
    bot.edit_message_text("🔎 <b>Анализ: Шаг 2/4 — Создаю ракурсы</b> (⏱️ ~30 сек)", chat_id, msg_id, parse_mode="HTML")
    left_card_url = retouch_photo(photo, style_key, "angle slightly from the left side, 3/4 view")
    right_card_url = retouch_photo(photo, style_key, "angle slightly from the right side, 3/4 view")
    far_left_card_url = retouch_photo(photo, style_key, "angle strongly from the left side, almost side view")
    far_right_card_url = retouch_photo(photo, style_key, "angle strongly from the right side, almost side view")
    
    bot.edit_message_text("🧠 <b>Анализ: Шаг 3/4 — Изучаю товар</b> (⏱️ ~25 сек)", chat_id, msg_id, parse_mode="HTML")
    analysis = deep_analyze_and_generate_questions(base_card_url)
    if not analysis:
        bot.edit_message_text("❌ <b>Ошибка:</b> не удалось проанализировать товар.", chat_id, msg_id, parse_mode="HTML")
        return
    
    user_data[user_id]['base_card'] = base_card_url
    user_data[user_id]['left_card'] = left_card_url
    user_data[user_id]['right_card'] = right_card_url
    user_data[user_id]['far_left_card'] = far_left_card_url
    user_data[user_id]['far_right_card'] = far_right_card_url
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