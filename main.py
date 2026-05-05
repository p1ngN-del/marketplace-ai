import os
import sys
import json
import base64
import sqlite3
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
        telebot.types.BotCommand("start", "Запустить бота"),
        telebot.types.BotCommand("admin", "Админ-панель"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        print(f"Не удалось установить команды: {e}")

set_bot_commands()

user_cards = {}
user_analysis = {}
user_states = {}

# --- СТИЛИ ФОНОВ ---
BG_STYLES = {
    "clean_white": {
        "name": "🤍 Чистый белый",
        "prompt": "Clean pure white studio background, professional product photography, soft shadows",
        "brightness": 250,
        "text_color": (30, 30, 30),
        "accent": (100, 100, 100),
    },
    "gradient_warm": {
        "name": "🧡 Теплый градиент",
        "prompt": "Warm gradient background from peach to cream, soft lighting, premium feel",
        "brightness": 200,
        "text_color": (60, 40, 20),
        "accent": (180, 100, 60),
    },
    "dark_luxury": {
        "name": "🖤 Темная роскошь",
        "prompt": "Dark charcoal background, dramatic lighting, luxury premium product photography",
        "brightness": 50,
        "text_color": (255, 255, 255),
        "accent": (180, 180, 180),
    },
    "mint_fresh": {
        "name": "💚 Мятная свежесть",
        "prompt": "Soft mint green background, fresh clean look, organic natural product feel",
        "brightness": 220,
        "text_color": (20, 60, 40),
        "accent": (80, 160, 120),
    },
    "sky_blue": {
        "name": "💙 Небесный",
        "prompt": "Soft sky blue gradient background, airy light feel, tech modern product",
        "brightness": 210,
        "text_color": (20, 40, 80),
        "accent": (80, 140, 220),
    },
    "rose_gold": {
        "name": "🩷 Розовое золото",
        "prompt": "Rose gold pink background, feminine elegant, soft pink and gold tones",
        "brightness": 200,
        "text_color": (80, 30, 40),
        "accent": (200, 100, 120),
    },
    "neon_tech": {
        "name": "💜 Неоновый",
        "prompt": "Dark background with neon purple and blue accents, cyberpunk tech style, glowing edges",
        "brightness": 60,
        "text_color": (255, 255, 255),
        "accent": (200, 100, 255),
    },
    "wood_natural": {
        "name": "🤎 Натуральное дерево",
        "prompt": "Light wood texture background, natural organic feel, warm tones, eco friendly",
        "brightness": 180,
        "text_color": (60, 40, 20),
        "accent": (160, 120, 80),
    },
}

# --- ФУНКЦИИ ОБРАБОТКИ ИЗОБРАЖЕНИЙ ---

def isolate_product(product_bytes):
    """Вырезает товар и возвращает его на прозрачном фоне"""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"
        prompt = "Isolate the product on a pure transparent background. Keep only the product itself, no shadows, no extra objects. High quality."
        
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
        print(f"Ошибка изоляции: {e}")
        return None
    return None

def generate_background(style_key="clean_white"):
    """Создаёт пустой фон в выбранном стиле"""
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        prompt = f"Empty {style['prompt']}. NO PRODUCT, just the background texture and lighting. High resolution."
        
        messages = [{"role": "user", "content": [{"text": prompt}]}]
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
        print(f"Ошибка создания фона: {e}")
        return None
    return None

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

def compose_card(product_url, bg_url, style_key="clean_white"):
    """Объединяет товар с фоном"""
    try:
        if product_url.startswith('http'):
            product_img = Image.open(BytesIO(requests.get(product_url, timeout=30).content)).convert('RGBA')
        else:
            product_img = Image.open(BytesIO(requests.get(product_url).content)).convert('RGBA')
            
        if bg_url.startswith('http'):
            bg_img = Image.open(BytesIO(requests.get(bg_url, timeout=30).content)).convert('RGBA')
        else:
            bg_img = Image.open(BytesIO(requests.get(bg_url).content)).convert('RGBA')

        # Масштабируем товар до 70% высоты фона
        target_height = int(bg_img.height * 0.7)
        ratio = target_height / product_img.height
        new_width = int(product_img.width * ratio)
        product_img = product_img.resize((new_width, target_height), Image.LANCZOS)

        # Размещаем по центру
        x = (bg_img.width - new_width) // 2
        y = (bg_img.height - target_height) // 2
        bg_img.paste(product_img, (x, y), product_img)

        return bg_img
    except Exception as e:
        print(f"Ошибка компоновки: {e}")
        return None

def add_infographic(base_image, title, features=None, bonuses=None, triggers=None, style_key="clean_white"):
    """Создаёт инфографику с полупрозрачными плашками в гамме фона"""
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        width, height = base_image.size
        
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Рассчитываем цвета плашек из гаммы фона
        r, g, b = style['text_color']
        plate_fill = (r, g, b, 70) # Полупрозрачный цвет текста
        plate_outline = (r, g, b, 40)
        
        margin = int(width * 0.05)
        plate_radius = int(height * 0.02)
        
        # Заголовок ПРЕМИУМ
        header_text = "ПРЕМИУМ КАЧЕСТВО"
        header_font = get_font(int(height * 0.025), 'regular')
        hw = draw.textbbox((0, 0), header_text, font=header_font)[2]
        draw.text(((width - hw) // 2, int(height * 0.03)), header_text, font=header_font, fill=style['text_color'])
        
        # Характеристики
        if features and len(features) > 0:
            badge_w = int(width * 0.38)
            badge_h = int(height * 0.10)
            start_y = int(height * 0.22)
            gap = int(height * 0.02)
            
            for i, feat in enumerate(features[:4]):
                bx = margin if i % 2 == 0 else width - margin - badge_w
                by = start_y + (i // 2) * (badge_h + gap)
                
                draw.rounded_rectangle(
                    [bx, by, bx + badge_w, by + badge_h],
                    radius=plate_radius,
                    fill=plate_fill,
                    outline=plate_outline,
                    width=1
                )
                
                value = feat.get('value', '')
                label = feat.get('label', '')
                display_value = value[:18] + ".." if len(value) > 18 else value
                
                val_font = get_font(int(height * 0.03), 'bold')
                label_font = get_font(int(height * 0.02), 'regular')
                
                draw.text((bx + 10, by + int(height * 0.02)), display_value, font=val_font, fill=style['text_color'])
                draw.text((bx + 10, by + int(height * 0.06)), label, font=label_font, fill=(r, g, b, 180))
        
        # Бонусы
        y_bonus = int(height * 0.68)
        if bonuses and len(bonuses) > 0:
            for bonus in bonuses[:2]:
                text = bonus[:35]
                bonus_font = get_font(int(height * 0.03), 'medium')
                tw = draw.textbbox((0, 0), text, font=bonus_font)[2] + 30
                bh = int(height * 0.06)
                
                draw.rounded_rectangle(
                    [((width - tw) // 2, y_bonus), ((width - tw) // 2 + tw, y_bonus + bh)],
                    radius=plate_radius,
                    fill=(r, g, b, 40),
                    outline=(r, g, b, 80),
                    width=1
                )
                
                draw.text(((width - tw) // 2 + 15, y_bonus + 8), text, font=bonus_font, fill=style['text_color'])
                y_bonus += bh + 10
        
        # Триггеры
        if triggers and len(triggers) > 0:
            for trigger in triggers[:2]:
                text = trigger[:35]
                trigger_font = get_font(int(height * 0.025), 'medium')
                tw = draw.textbbox((0, 0), text, font=trigger_font)[2] + 30
                th = int(height * 0.05)
                
                draw.rounded_rectangle(
                    [((width - tw) // 2, y_bonus), ((width - tw) // 2 + tw, y_bonus + th)],
                    radius=plate_radius,
                    fill=(r, g, b, 50),
                    outline=(r, g, b, 100),
                    width=1
                )
                
                draw.text(((width - tw) // 2 + 15, y_bonus + 6), text, font=trigger_font, fill=style['text_color'])
                y_bonus += th + 8
        
        # Заголовок
        if title:
            title_text = title.upper()[:40]
            title_font = get_font(int(height * 0.07), 'bold')
            tw = draw.textbbox((0, 0), title_text, font=title_font)[2]
            
            plate_pad = 12
            px = (width - tw) // 2 - plate_pad
            py = int(height * 0.88) - plate_pad
            
            draw.rounded_rectangle(
                [px, py, px + tw + plate_pad * 2, py + int(height * 0.07) + plate_pad * 2],
                radius=plate_radius,
                fill=(r, g, b, 40),
            )
            
            shadow_color = (0, 0, 0, 60)
            draw.text((px + plate_pad + 2, py + plate_pad + 2), title_text, font=title_font, fill=shadow_color)
            draw.text((px + plate_pad, py + plate_pad), title_text, font=title_font, fill=style['text_color'])
        
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
        import traceback
        traceback.print_exc()
        return None

# === ОБРАБОТЧИКИ ===

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    is_admin = user_id == str(ADMIN_ID)
    
    welcome = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 <b>Отправьте фото товара</b> — я:\n"
        "✨ Уберу лишние объекты\n"
        "🎨 Создам студийный фон\n"
        "📝 Добавлю инфографику\n\n"
        "💡 <b>Как это работает:</b>\n"
        "1. Отправляете фото\n"
        "2. Выбираете стиль фона\n"
        "3. Отвечаете на вопросы (или пропускаете)\n"
        "4. Получаете готовую карточку!"
    )
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        telebot.types.KeyboardButton("📸 Создать карточку"),
        telebot.types.KeyboardButton("❓ Помощь"),
    ]
    if is_admin:
        buttons.append(telebot.types.KeyboardButton("📊 Админ-панель"))
    
    markup.add(*buttons)
    bot.send_message(message.chat.id, welcome, parse_mode="HTML", reply_markup=markup)

def admin_stats(message):
    user_id = str(message.from_user.id)
    if user_id != str(ADMIN_ID):
        bot.send_message(message.chat.id, "⛔ Нет доступа.")
        return
    
    total_users, total_requests, recent_users = get_stats()
    text = f"📊 <b>Админ-панель</b>\n\n👥 Всего: <b>{total_users}</b>\n📸 Запросов: <b>{total_requests}</b>\n\n"
    
    for i, u in enumerate(recent_users, 1):
        name = u[3] or "—"
        username = f"@{u[2]}" if u[2] else "—"
        text += f"{i}. {name} ({username}) — {u[7]} запросов\n"
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="admin_close"))
    
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)

# === ОБРАБОТКА ФОТО ===

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for key, style in BG_STYLES.items():
        markup.add(telebot.types.InlineKeyboardButton(style['name'], callback_data=f"style_{key}"))
    
    bot.send_message(
        message.chat.id,
        "🎨 <b>Выберите стиль фона:</b>",
        parse_mode="HTML",
        reply_markup=markup
    )
    
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    user_cards[user_id] = {
        'photo': downloaded_file,
        'style': None,
        'step': 'style_selection'
    }

# === CALLBACK ОБРАБОТЧИК ===

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = str(call.from_user.id)
    data = call.data
    
    if data.startswith("style_"):
        style_key = data.replace("style_", "")
        if user_id in user_cards:
            user_cards[user_id]['style'] = style_key
        
        bot.answer_callback_query(call.id, "✅ Стиль выбран")
        
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("🤖 AI-вопросы (5 вопросов)", callback_data="mode_ai"),
            telebot.types.InlineKeyboardButton("🛠️ Конструктор (ручной)", callback_data="mode_manual"),
            telebot.types.InlineKeyboardButton("✨ Авто-генерация", callback_data="mode_auto")
        )
        
        bot.edit_message_text(
            "🎯 <b>Выберите режим:</b>\n\n"
            "🤖 AI задаст 5 вопросов и создаст идеальную карточку\n"
            "🛠️ Вы сами выберете все элементы\n"
            "✨ AI всё сделает автоматически",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup
        )
        return
    
    elif data == "mode_ai":
        bot.answer_callback_query(call.id, "🤖 Запускаем AI-вопросы")
        
        wait_msg = bot.send_message(call.message.chat.id, "⏳ Анализируем товар...")
        
        card_data = user_cards.get(user_id, {})
        style_key = card_data.get('style', 'clean_white')
        
        # Изолируем товар
        isolated = isolate_product(card_data['photo'])
        if not isolated:
            bot.edit_message_text("❌ Ошибка обработки", call.message.chat.id, wait_msg.message_id)
            return
        
        card_data['isolated'] = isolated
        user_cards[user_id] = card_data
        
        analysis = analyze_product(isolated)
        user_analysis[user_id] = analysis or {
            'questions': [
                "Из какого материала товар?",
                "С какими моделями совместим?",
                "Какой бонус вы даете?",
                "Какая акция или скидка?",
                "Чем ваш товар лучше конкурентов?"
            ]
        }
        
        bot.delete_message(call.message.chat.id, wait_msg.message_id)
        
        questions = user_analysis[user_id].get('questions', [])
        if questions:
            start_ai_questions(call.message.chat.id, user_id, questions)
        return
    
    elif data == "skip_question":
        if user_id in user_analysis:
            if 'answers' not in user_analysis[user_id]:
                user_analysis[user_id]['answers'] = []
            user_analysis[user_id]['answers'].append("")
            
            current = user_analysis[user_id].get('current_q', 0)
            questions = user_analysis[user_id].get('questions', [])
            
            if current + 1 < len(questions):
                user_analysis[user_id]['current_q'] = current + 1
                ask_question(call.message.chat.id, user_id, current + 1, questions)
            else:
                finish_ai_mode(call.message.chat.id, user_id)
        
        bot.answer_callback_query(call.id, "⏭️ Пропущено")
        return
    
    elif data == "mode_manual":
        bot.answer_callback_query(call.id, "🛠️ Открываем конструктор")
        start_constructor(call.message.chat.id, user_id)
        return
    
    elif data == "mode_auto":
        bot.answer_callback_query(call.id, "✨ Генерируем...")
        generate_auto(call.message.chat.id, user_id)
        return
    
    elif data == "admin_refresh":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Нет доступа")
            return
        bot.answer_callback_query(call.id, "🔄 Обновляем")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        admin_stats(call.message)
        return
    
    elif data == "admin_close":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Нет доступа")
            return
        bot.answer_callback_query(call.id, "✅ Закрыто")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return

# === AI ВОПРОСЫ ===

def start_ai_questions(chat_id, user_id, questions):
    user_analysis[user_id]['current_q'] = 0
    user_analysis[user_id]['answers'] = []
    ask_question(chat_id, user_id, 0, questions)

def ask_question(chat_id, user_id, q_index, questions):
    if q_index >= len(questions):
        finish_ai_mode(chat_id, user_id)
        return
    
    question = questions[q_index]
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⏭️ Пропустить вопрос", callback_data="skip_question"))
    
    msg = bot.send_message(
        chat_id,
        f"🤖 <b>Вопрос {q_index + 1} из {len(questions)}:</b>\n\n{question}\n\n"
        f"Ответьте текстом или нажмите «Пропустить»",
        parse_mode="HTML",
        reply_markup=markup
    )
    
    bot.register_next_step_handler(msg, process_ai_answer, user_id, questions)

def process_ai_answer(message, user_id, questions):
    chat_id = message.chat.id
    
    if user_id not in user_analysis:
        bot.send_message(chat_id, "❌ Сессия истекла")
        return
    
    user_analysis[user_id]['answers'].append(message.text.strip())
    current = user_analysis[user_id]['current_q']
    
    if current + 1 < len(questions):
        user_analysis[user_id]['current_q'] = current + 1
        ask_question(chat_id, user_id, current + 1, questions)
    else:
        finish_ai_mode(chat_id, user_id)

def finish_ai_mode(chat_id, user_id):
    bot.send_message(chat_id, "⏳ Создаём инфографику...")
    
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    card_data = user_cards.get(user_id, {})
    isolated_url = card_data.get('isolated')
    style_key = card_data.get('style', 'clean_white')
    
    # Генерируем фон и собираем карточку
    bg_url = generate_background(style_key)
    if not bg_url:
        bot.send_message(chat_id, "❌ Ошибка создания фона")
        return
    
    base_card = compose_card(isolated_url, bg_url, style_key)
    if not base_card:
        bot.send_message(chat_id, "❌ Ошибка сборки карточки")
        return
    
    features = []
    bonuses = []
    triggers = []
    title = "ПРЕМИУМ ТОВАР"
    
    for i, ans in enumerate(answers):
        if not ans:
            continue
        
        if i == 0 and len(ans) > 2:
            features.append({"icon": "🔷", "label": "Материал", "value": ans[:20]})
            if len(ans) < 20:
                title = ans.upper()
        elif i == 1 and len(ans) > 2:
            features.append({"icon": "✅", "label": "Совместимость", "value": ans[:20]})
        elif i == 2 and len(ans) > 2:
            bonuses.append(f"🎁 {ans[:25]}")
        elif i == 3 and len(ans) > 2:
            triggers.append(f"⏰ {ans[:25]}")
        elif i == 4 and len(ans) > 2:
            if len(ans) < 15:
                title = ans.upper()
            else:
                features.append({"icon": "⭐", "label": "Преимущество", "value": ans[:20]})
    
    if len(features) < 2:
        features.append({"icon": "📦", "label": "Категория", "value": analysis.get('category', 'Товар')[:20]})
    if not bonuses:
        bonuses.append("🚚 Быстрая доставка")
    if not triggers:
        triggers.append("🔥 Хит продаж")
    
    # Генерируем 3 варианта карточек
    cards = []
    
    # Карточка 1: Название
    card1 = add_infographic(base_card.copy(), title, features[:2], None, None, style_key)
    if card1:
        cards.append(card1)
    
    # Карточка 2: Характеристики
    card2 = add_infographic(base_card.copy(), None, features, None, None, style_key)
    if card2:
        cards.append(card2)
    
    # Карточка 3: Бонусы и триггеры
    card3 = add_infographic(base_card.copy(), None, None, bonuses, triggers, style_key)
    if card3:
        cards.append(card3)
    
    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/3")
    else:
        bot.send_message(chat_id, "❌ Ошибка генерации")

# === КОНСТРУКТОР ===

def generate_auto(chat_id, user_id):
    card_data = user_cards.get(user_id, {})
    style_key = card_data.get('style', 'clean_white')
    isolated_url = card_data.get('isolated')
    
    if not isolated_url:
        isolated_url = isolate_product(card_data.get('photo'))
        if not isolated_url:
            bot.send_message(chat_id, "❌ Ошибка обработки фото")
            return
        card_data['isolated'] = isolated_url
        user_cards[user_id] = card_data
    
    bg_url = generate_background(style_key)
    if not bg_url:
        bot.send_message(chat_id, "❌ Ошибка создания фона")
        return
    
    base_card = compose_card(isolated_url, bg_url, style_key)
    if not base_card:
        bot.send_message(chat_id, "❌ Ошибка сборки карточки")
        return
    
    features = [
        {"icon": "⭐", "label": "Качество", "value": "Премиум"},
        {"icon": "🚚", "label": "Доставка", "value": "Бесплатно"}
    ]
    bonuses = ["🎁 Подарок при заказе"]
    triggers = ["🔥 Хит продаж"]
    
    cards = []
    
    card1 = add_infographic(base_card.copy(), "ПРЕМИУМ ТОВАР", features[:2], None, None, style_key)
    if card1:
        cards.append(card1)
    
    card2 = add_infographic(base_card.copy(), None, features, None, None, style_key)
    if card2:
        cards.append(card2)
    
    card3 = add_infographic(base_card.copy(), None, None, bonuses, triggers, style_key)
    if card3:
        cards.append(card3)
    
    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/3")
    else:
        bot.send_message(chat_id, "❌ Ошибка генерации")

def start_constructor(chat_id, user_id):
    card_data = user_cards.get(user_id, {})
    style_key = card_data.get('style', 'clean_white')
    
    # Изолируем товар, если ещё не
    isolated_url = card_data.get('isolated')
    if not isolated_url:
        wait_msg = bot.send_message(chat_id, "⏳ Обрабатываем фото...")
        isolated_url = isolate_product(card_data.get('photo'))
        if not isolated_url:
            bot.edit_message_text("❌ Ошибка обработки", chat_id, wait_msg.message_id)
            return
        card_data['isolated'] = isolated_url
        user_cards[user_id] = card_data
        bot.delete_message(chat_id, wait_msg.message_id)
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("📝 Заголовок", callback_data="con_title"),
        telebot.types.InlineKeyboardButton("🔷 Характеристики (до 4)", callback_data="con_features"),
        telebot.types.InlineKeyboardButton("🎁 Бонусы", callback_data="con_bonuses"),
        telebot.types.InlineKeyboardButton("⏰ Акции", callback_data="con_triggers"),
        telebot.types.InlineKeyboardButton("✨ Создать карточку", callback_data="con_generate")
    )
    
    user_states[user_id] = {
        'mode': 'constructor',
        'title': '',
        'features': [],
        'bonuses': [],
        'triggers': []
    }
    
    bot.send_message(
        chat_id,
        "🛠️ <b>Конструктор карточки</b>\n\n"
        "Выберите элементы для добавления.\n"
        "Когда всё будет готово — нажмите «Создать карточку».",
        parse_mode="HTML",
        reply_markup=markup
    )

# === ТЕКСТОВЫЕ КНОПКИ ===

@bot.message_handler(func=lambda m: m.text == "📸 Создать карточку")
def btn_create(message):
    bot.send_message(message.chat.id, "Отправьте фото товара")

@bot.message_handler(func=lambda m: m.text == "❓ Помощь")
def btn_help(message):
    bot.send_message(
        message.chat.id,
        "Отправьте фото → выберите стиль → выберите режим (AI/конструктор/авто)"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Админ-панель")
def btn_admin(message):
    admin_stats(message)

# === WEBHOOK ===

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return '', 403

@app.route('/')
def index():
    return "Бот работает"

@app.route('/health')
def health():
    return {'status': 'ok'}, 200

# === ЗАПУСК ===

if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
    
    print("Бот запущен!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
