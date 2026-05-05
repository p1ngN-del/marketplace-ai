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

# Установка команд меню
def set_bot_commands():
    commands = [
        telebot.types.BotCommand("start", "Zapustit bota"),
        telebot.types.BotCommand("admin", "Admin-panel"),
    ]
    try:
        bot.set_my_commands(commands)
        print("Komandy meny ustanovleny")
    except Exception as e:
        print(f"Ne udalos ustanovit komandy: {e}")

set_bot_commands()

# Временное хранилище
user_cards = {}
user_analysis = {}
user_states = {}  # Для отслеживания состояния конструктора

# --- СТИЛИ ФОНОВ ---
BG_STYLES = {
    "clean_white": {
        "name": "🤍 Чистый белый",
        "prompt": "Clean pure white studio background, professional product photography, soft shadows",
        "brightness": 250,
        "text_color": (30, 30, 30),
        "plate_color": (255, 255, 255, 180),
        "plate_border": (200, 200, 200, 120),
        "accent": (255, 100, 50),
    },
    "gradient_warm": {
        "name": "🧡 Теплый градиент",
        "prompt": "Warm gradient background from peach to cream, soft lighting, premium feel",
        "brightness": 200,
        "text_color": (60, 40, 20),
        "plate_color": (255, 250, 240, 190),
        "plate_border": (255, 200, 150, 100),
        "accent": (255, 120, 0),
    },
    "dark_luxury": {
        "name": "🖤 Темная роскошь",
        "prompt": "Dark charcoal background, dramatic lighting, luxury premium product photography, gold accents",
        "brightness": 50,
        "text_color": (255, 255, 255),
        "plate_color": (40, 40, 40, 200),
        "plate_border": (100, 100, 100, 150),
        "accent": (255, 200, 100),
    },
    "mint_fresh": {
        "name": "💚 Мятная свежесть",
        "prompt": "Soft mint green background, fresh clean look, organic natural product feel",
        "brightness": 220,
        "text_color": (20, 60, 40),
        "plate_color": (240, 255, 250, 190),
        "plate_border": (150, 220, 180, 100),
        "accent": (0, 150, 100),
    },
    "sky_blue": {
        "name": "💙 Небесный",
        "prompt": "Soft sky blue gradient background, airy light feel, tech modern product",
        "brightness": 210,
        "text_color": (20, 40, 80),
        "plate_color": (240, 250, 255, 190),
        "plate_border": (150, 200, 255, 100),
        "accent": (0, 100, 200),
    },
    "rose_gold": {
        "name": "🩷 Розовое золото",
        "prompt": "Rose gold pink background, feminine elegant, soft pink and gold tones",
        "brightness": 200,
        "text_color": (80, 30, 40),
        "plate_color": (255, 240, 245, 190),
        "plate_border": (255, 180, 200, 100),
        "accent": (200, 80, 100),
    },
    "neon_tech": {
        "name": "💜 Неоновый",
        "prompt": "Dark background with neon purple and blue accents, cyberpunk tech style, glowing edges",
        "brightness": 60,
        "text_color": (255, 255, 255),
        "plate_color": (20, 10, 40, 200),
        "plate_border": (150, 50, 255, 150),
        "accent": (180, 50, 255),
    },
    "wood_natural": {
        "name": "🤎 Натуральное дерево",
        "prompt": "Light wood texture background, natural organic feel, warm tones, eco friendly",
        "brightness": 180,
        "text_color": (60, 40, 20),
        "plate_color": (255, 250, 240, 190),
        "plate_border": (200, 180, 150, 100),
        "accent": (139, 90, 43),
    },
}

# --- ФУНКЦИИ ОБРАБОТКИ ИЗОБРАЖЕНИЙ ---

def retouch_photo(product_bytes, style_key="clean_white"):
    """Ретушь фото с выбранным стилем фона"""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        
        prompt = f"Remove all extra objects from photo. Keep only the product. Place it on: {style['prompt']}. Professional studio lighting, high quality."
        
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
        print(f"Oshibka retushi: {e}")
        return None
    return None

def create_card(product_url, style_key="clean_white"):
    """Создание карточки с выбранным стилем"""
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        prompt = f"Create premium product card for marketplace. {style['prompt']}. Product centered, perfect lighting. NO TEXT, NO WATERMARK."
        
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
        print(f"Oshibka sozdaniya kartochki: {e}")
        return None
    return None

def analyze_product(image_url):
    """Gлубокий анализ товара"""
    try:
        prompt = """Analyze this product photo. Identify:
1. Product category and type
2. 3-5 key features buyers care about
3. Pain points this product solves
4. Target audience (age, gender, interests)
5. Best selling angles for this product

Return ONLY JSON format:
{"category":"...","product_name":"...","key_features":["..."],"pain_points":["..."],"target_audience":"...","best_accents":["..."],"questions":["What material is it made of?","What models is it compatible with?","What bonus do you offer?","What discount or promotion?","What makes your product better than competitors?"]}"""
        
        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]
        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-vl-max",
            messages=messages
        )
        
        if response.status_code == 200:
            text = response.output.choices[0].message.content[0]['text']
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
    except Exception as e:
        print(f"Oshibka analiza: {e}")
        return None
    return None

def get_font(size, weight='regular'):
    fonts = {
        'bold': [
            '/app/Montserrat-Bold.ttf',
            '/app/montserrat-bold.ttf',
            '/app/font_bold.ttf',
        ],
        'medium': [
            '/app/Montserrat-Medium.ttf',
            '/app/montserrat-medium.ttf',
            '/app/font.ttf',
        ],
        'regular': [
            '/app/Montserrat-Regular.ttf',
            '/app/montserrat-regular.ttf',
            '/app/font_regular.ttf',
        ]
    }
    
    font_list = fonts.get(weight, fonts['regular'])
    
    for font_path in font_list:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size)
                # Правильная проверка кириллицы
                test_bbox = font.getbbox("ЙЦУКЕНГШЩЗ")
                if test_bbox and (test_bbox[2] - test_bbox[0]) > 50:
                    print(f"✅ Шрифт загружен: {font_path}")
                    return font
                else:
                    print(f"⚠️ Шрифт без кириллицы: {font_path}")
            except Exception as e:
                print(f"❌ Ошибка шрифта {font_path}: {e}")
                continue
    
    # Системный fallback
    for fp in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
               '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    
    print("⚠️ ВНИМАНИЕ: Используется стандартный шрифт (кириллица не гарантируется)")
    return ImageFont.load_default()

def add_infographic(image_url, title, features=None, bonuses=None, triggers=None, style_key="clean_white"):
    """Профессиональная инфографика с полупрозрачными плашками"""
    try:
        # Load image
        if image_url.startswith('http'):
            response = requests.get(image_url, timeout=30)
            img = Image.open(BytesIO(response.content))
        elif image_url.startswith('data:image'):
            base64_data = image_url.split(',')[1]
            img_data = base64.b64decode(base64_data)
            img = Image.open(BytesIO(img_data))
        else:
            img = Image.open(image_url)
        
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        
        width, height = img.size
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        
        # Create overlay
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Fonts - adaptive sizing
        def get_fit_font(text, max_width, start_size, weight='bold'):
            size = start_size
            while size > 12:
                font = get_font(size, weight)
                bbox = draw.textbbox((0, 0), text, font=font)
                w = bbox[2] - bbox[0]
                if w <= max_width:
                    return font, size, w
                size -= 2
            return get_font(12, weight), 12, 0
        
        margin = int(width * 0.05)
        plate_radius = int(height * 0.015)
        
        # === HEADER: Small label ===
        header_text = "PREMIUM QUALITY"
        header_font, _, hw = get_fit_font(header_text, width * 0.6, int(height * 0.022), 'regular')
        hx = (width - hw) // 2
        hy = int(height * 0.02)
        
        # Clean minimal header - no artifacts
        draw.text((hx, hy), header_text, font=header_font, fill=style['accent'])
        
        y_pos = int(height * 0.06)
        
        # === FEATURES: Side badges ===
        if features and len(features) > 0:
            badge_w = int(width * 0.38)
            badge_h = int(height * 0.09)
            start_y = int(height * 0.22)
            gap = int(height * 0.02)
            
            for i, feat in enumerate(features[:4]):
                # Alternate left/right
                if i % 2 == 0:
                    bx = margin
                else:
                    bx = width - margin - badge_w
                
                by = start_y + (i // 2) * (badge_h + gap)
                
                # Semi-transparent plate
                draw.rounded_rectangle(
                    [bx, by, bx + badge_w, by + badge_h],
                    radius=plate_radius,
                    fill=style['plate_color'],
                    outline=style['plate_border'],
                    width=1
                )
                
                # Icon + text
                icon = feat.get('icon', '')
                label = feat.get('label', '')
                value = feat.get('value', '')
                
                # Icon
                icon_font = get_font(int(height * 0.03), 'regular')
                draw.text((bx + 8, by + 5), icon, font=icon_font, fill=style['accent'])
                
                # Label (small)
                label_font = get_font(int(height * 0.018), 'regular')
                draw.text((bx + 8, by + int(height * 0.038)), label, 
                         font=label_font, fill=(*style['text_color'][:3], 180))
                
                # Value (main) - truncate if too long
                val_font, val_size, _ = get_fit_font(value, badge_w - 16, int(height * 0.028), 'medium')
                display_value = value
                if len(value) > 20:
                    display_value = value[:18] + ".."
                
                draw.text((bx + 8, by + int(height * 0.055)), display_value, 
                         font=val_font, fill=style['text_color'])
        
        # === BONUSES: Bottom plates ===
        y_bonus = int(height * 0.68)
        if bonuses and len(bonuses) > 0:
            for bonus in bonuses[:2]:
                text = bonus[:30]  # Limit length
                bfont, bsize, bw = get_fit_font(text, width * 0.8, int(height * 0.03), 'medium')
                
                bw = min(bw + 20, width - margin * 2)
                bh = int(height * 0.055)
                bx = (width - bw) // 2
                
                # Green-tinted plate for bonuses
                bonus_plate = (*style['plate_color'][:3], 200)
                draw.rounded_rectangle(
                    [bx, y_bonus, bx + bw, y_bonus + bh],
                    radius=plate_radius,
                    fill=bonus_plate,
                    outline=(*style['accent'][:3], 100),
                    width=1
                )
                
                draw.text((bx + 10, y_bonus + 6), text, 
                         font=bfont, fill=style['text_color'])
                
                y_bonus += bh + 8
        
        # === TRIGGERS: Urgency plates ===
        if triggers and len(triggers) > 0:
            for trigger in triggers[:2]:
                text = trigger[:30]
                tfont, tsize, tw = get_fit_font(text, width * 0.8, int(height * 0.025), 'medium')
                
                tw = min(tw + 16, width - margin * 2)
                th = int(height * 0.045)
                tx = (width - tw) // 2
                
                # Red-tinted for urgency
                trigger_plate = (255, 240, 240, 180) if style['brightness'] > 150 else (60, 20, 20, 180)
                trigger_text = (150, 30, 30) if style['brightness'] > 150 else (255, 100, 100)
                
                draw.rounded_rectangle(
                    [tx, y_bonus, tx + tw, y_bonus + th],
                    radius=plate_radius,
                    fill=trigger_plate,
                    outline=(200, 100, 100, 100),
                    width=1
                )
                
                draw.text((tx + 8, y_bonus + 5), text, 
                         font=tfont, fill=trigger_text)
                
                y_bonus += th + 6
        
        # === MAIN TITLE ===
        if title:
            title_y = min(y_bonus + 10, int(height * 0.88))
            title_text = title.upper()[:40]  # Limit
            
            # Fit to width
            tfont, tsize, tw = get_fit_font(title_text, width * 0.9, int(height * 0.065), 'bold')
            
            # Shadow plate behind title
            plate_pad = 8
            px = (width - tw) // 2 - plate_pad
            py = title_y - plate_pad
            
            draw.rounded_rectangle(
                [px, py, px + tw + plate_pad * 2, py + tsize + plate_pad * 2],
                radius=plate_radius,
                fill=(*style['text_color'][:3], 40) if style['brightness'] > 150 else (255, 255, 255, 30),
            )
            
            # Text with slight shadow for depth
            shadow_color = (0, 0, 0, 60) if style['brightness'] > 150 else (0, 0, 0, 80)
            draw.text((px + plate_pad + 1, py + plate_pad + 1), title_text, 
                     font=tfont, fill=shadow_color)
            draw.text((px + plate_pad, py + plate_pad), title_text, 
                     font=tfont, fill=style['text_color'])
        
        # Composite
        final = Image.alpha_composite(img, overlay)
        final = final.convert('RGB')
        
        # Enhance
        enhancer = ImageEnhance.Contrast(final)
        final = enhancer.enhance(1.1)
        
        output = BytesIO()
        final.save(output, format='JPEG', quality=95)
        output.seek(0)
        return output
        
    except Exception as e:
        print(f"Oshibka infografiki: {e}")
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
        bot.send_message(message.chat.id, "⛔ Net dostupa.")
        return
    
    total_users, total_requests, recent_users = get_stats()
    text = f"📊 <b>Admin-panel</b>\n\n👥 Vsego: <b>{total_users}</b>\n📸 Zaprosov: <b>{total_requests}</b>\n\n"
    
    for i, u in enumerate(recent_users, 1):
        name = u[3] or "—"
        username = f"@{u[2]}" if u[2] else "—"
        text += f"{i}. {name} ({username}) — {u[7]} zaprosov\n"
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔄 Obnovit", callback_data="admin_refresh"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Zakryt", callback_data="admin_close"))
    
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)

# === ОБРАБОТКА ФОТО ===

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    # Сначала выбор стиля
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for key, style in BG_STYLES.items():
        markup.add(telebot.types.InlineKeyboardButton(style['name'], callback_data=f"style_{key}"))
    
    bot.send_message(
        message.chat.id,
        "🎨 <b>Vyberite stil fona:</b>",
        parse_mode="HTML",
        reply_markup=markup
    )
    
    # Сохраняем фото для последующей обработки
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
    
    # --- ВЫБОР СТИЛЯ ---
    if data.startswith("style_"):
        style_key = data.replace("style_", "")
        if user_id in user_cards:
            user_cards[user_id]['style'] = style_key
        
        bot.answer_callback_query(call.id, "✅ Stil vybran")
        
        # Показываем варианты работы
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("🤖 AI-voprosy (5 voprosov)", callback_data="mode_ai"),
            telebot.types.InlineKeyboardButton("🛠️ Konstruktor (ruchnoy)", callback_data="mode_manual"),
            telebot.types.InlineKeyboardButton("✨ Avto-generatsiya", callback_data="mode_auto")
        )
        
        bot.edit_message_text(
            "🎯 <b>Vyberite rezhim:</b>\n\n"
            "🤖 AI zadast 5 voprosov i sozdat idealnuyu kartochku\n"
            "🛠️ Vy sami vyberete vse elementy\n"
            "✨ AI vse sdelaet avtomaticheski",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup
        )
        return
    
    # --- AI ВОПРОСЫ ---
    elif data == "mode_ai":
        bot.answer_callback_query(call.id, "🤖 Zapuskaem AI-voprosy")
        
        # Анализируем товар
        wait_msg = bot.send_message(call.message.chat.id, "⏳ Analiziruem tovar...")
        
        card_data = user_cards.get(user_id, {})
        style_key = card_data.get('style', 'clean_white')
        
        # Сначала ретушь
        retouched = retouch_photo(card_data['photo'], style_key)
        if not retouched:
            bot.edit_message_text("❌ Oshibka obrabotki", call.message.chat.id, wait_msg.message_id)
            return
        
        # Карточка
        card_url = create_card(retouched, style_key)
        if not card_url:
            bot.edit_message_text("❌ Oshibka sozdaniya", call.message.chat.id, wait_msg.message_id)
            return
        
        card_data['card_url'] = card_url
        user_cards[user_id] = card_data
        
        # Анализ
        analysis = analyze_product(card_url)
        user_analysis[user_id] = analysis or {
            'questions': [
                "Iz kakogo materiala tovar?",
                "S kakimi modelami sovmestim?",
                "Kakoy bonus vy daete?",
                "Kakaya aktsiya ili skidka?",
                "Chem vash tovar luchshe konkurentov?"
            ]
        }
        
        bot.delete_message(call.message.chat.id, wait_msg.message_id)
        
        # Первый вопрос
        questions = user_analysis[user_id].get('questions', [])
        if questions:
            start_ai_questions(call.message.chat.id, user_id, questions)
        return
    
    # --- ПРОПУСК ВОПРОСА ---
    elif data == "skip_question":
        # Сохраняем пустой ответ
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
        
        bot.answer_callback_query(call.id, "⏭️ Propuscheno")
        return
    
    # --- РЕЖИМ КОНСТРУКТОРА ---
    elif data == "mode_manual":
        bot.answer_callback_query(call.id, "🛠️ Otkryvaem konstruktor")
        start_constructor(call.message.chat.id, user_id)
        return
    
    # --- АВТО РЕЖИМ ---
    elif data == "mode_auto":
        bot.answer_callback_query(call.id, "✨ Generiruem...")
        generate_auto(call.message.chat.id, user_id)
        return
    
    # --- АДМИН ---
    elif data == "admin_refresh":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Net dostupa")
            return
        bot.answer_callback_query(call.id, "🔄 Obnovlyaem")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        admin_stats(call.message)
        return
    
    elif data == "admin_close":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Net dostupa")
            return
        bot.answer_callback_query(call.id, "✅ Zakryto")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return

# === AI ВОПРОСЫ ===

def start_ai_questions(chat_id, user_id, questions):
    """Начинаем цикл вопросов"""
    user_analysis[user_id]['current_q'] = 0
    user_analysis[user_id]['answers'] = []
    ask_question(chat_id, user_id, 0, questions)

def ask_question(chat_id, user_id, q_index, questions):
    """Задаём вопрос с кнопкой пропуска"""
    if q_index >= len(questions):
        finish_ai_mode(chat_id, user_id)
        return
    
    question = questions[q_index]
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("⏭️ Propustit vopros", callback_data="skip_question"))
    
    msg = bot.send_message(
        chat_id,
        f"🤖 <b>Vopros {q_index + 1} iz {len(questions)}:</b>\n\n{question}\n\n"
        f"Otvette tekstom ili nazhmite 'Propustit'",
        parse_mode="HTML",
        reply_markup=markup
    )
    
    # Регистрируем обработчик
    bot.register_next_step_handler(msg, process_ai_answer, user_id, questions)

def process_ai_answer(message, user_id, questions):
    """Обработка ответа на вопрос AI"""
    chat_id = message.chat.id
    
    if user_id not in user_analysis:
        bot.send_message(chat_id, "❌ Sessiya istekla")
        return
    
    # Сохраняем ответ
    user_analysis[user_id]['answers'].append(message.text.strip())
    current = user_analysis[user_id]['current_q']
    
    if current + 1 < len(questions):
        user_analysis[user_id]['current_q'] = current + 1
        ask_question(chat_id, user_id, current + 1, questions)
    else:
        finish_ai_mode(chat_id, user_id)

def finish_ai_mode(chat_id, user_id):
    """Финальная генерация после всех вопросов"""
    bot.send_message(chat_id, "⏳ Sozdaem infografiku...")
    
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    card_data = user_cards.get(user_id, {})
    card_url = card_data.get('card_url')
    style_key = card_data.get('style', 'clean_white')
    
    # Формируем данные из ответов
    features = []
    bonuses = []
    triggers = []
    title = "PREMIUM TOVAR"
    
    # Парсим ответы
    for i, ans in enumerate(answers):
        if not ans:  # Пропущенный вопрос
            continue
            
        ans_lower = ans.lower()
        
        if i == 0 and len(ans) > 2:  # Материал
            features.append({"icon": "🔷", "label": "Material", "value": ans[:20]})
            title = ans[:25].upper()
        
        elif i == 1 and len(ans) > 2:  # Совместимость
            features.append({"icon": "✅", "label": "Sovmestimost", "value": ans[:20]})
        
        elif i == 2 and len(ans) > 2:  # Бонус
            bonuses.append(f"🎁 {ans[:25]}")
        
        elif i == 3 and len(ans) > 2:  # Акция
            triggers.append(f"⏰ {ans[:25]}")
        
        elif i == 4 and len(ans) > 2:  # Преимущество
            if len(ans) < 15:
                title = ans.upper()
            else:
                features.append({"icon": "⭐", "label": "Preimushchestvo", "value": ans[:20]})
    
    # Дефолты если мало данных
    if len(features) < 2:
        features.append({"icon": "📦", "label": "Kategoriya", "value": analysis.get('category', 'Tovar')[:20]})
    if not bonuses:
        bonuses.append("🚚 Bystraya dostavka")
    if not triggers:
        triggers.append("🔥 Hit prodazh")
    
    # Генерируем
    if card_url:
        final = add_infographic(card_url, title, features, bonuses, triggers, style_key)
        if final:
            bot.send_photo(chat_id, final, caption="✅ <b>Infografika gotova!</b>", parse_mode="HTML")
        else:
            bot.send_message(chat_id, "❌ Oshibka generatsii")
    else:
        bot.send_message(chat_id, "❌ Kartochka ne naydena")

# === КОНСТРУКТОР ===

def generate_auto(chat_id, user_id):
    """Автоматическая генерация инфографики"""
    card_data = user_cards.get(user_id, {})
    style_key = card_data.get('style', 'clean_white')
    card_url = card_data.get('card_url')
    
    if not card_url:
        # Создаём карточку, если ещё нет
        retouched = retouch_photo(card_data.get('photo'), style_key)
        if retouched:
            card_url = create_card(retouched, style_key)
            user_cards[user_id]['card_url'] = card_url
    
    if card_url:
        # Простая инфографика без вопросов
        features = [
            {"icon": "⭐", "label": "Качество", "value": "Премиум"},
            {"icon": "🚚", "label": "Доставка", "value": "Бесплатно"}
        ]
        bonuses = ["🎁 Подарок при заказе"]
        triggers = ["🔥 Хит продаж"]
        
        final = add_infographic(card_url, "ПРЕМИУМ ТОВАР", features, bonuses, triggers, style_key)
        if final:
            bot.send_photo(chat_id, final, caption="✅ <b>Инфографика готова!</b>", parse_mode="HTML")
        else:
            bot.send_message(chat_id, "❌ Ошибка генерации")
    else:
        bot.send_message(chat_id, "❌ Карточка не найдена")

def start_constructor(chat_id, user_id):
    """Детальный конструктор карточки"""
    card_data = user_cards.get(user_id, {})
    style_key = card_data.get('style', 'clean_white')
    
    # Сначала делаем базовую карточку
    wait_msg = bot.send_message(chat_id, "⏳ Sozdaem bazovuyu kartochku...")
    
    retouched = retouch_photo(card_data['photo'], style_key)
    if retouched:
        card_url = create_card(retouched, style_key)
        card_data['card_url'] = card_url
        user_cards[user_id] = card_data
    
    bot.delete_message(chat_id, wait_msg.message_id)
    
    # Меню конструктора
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("📝 Zagolovok", callback_data="con_title"),
        telebot.types.InlineKeyboardButton("🔷 Kharakteristiki (do 4)", callback_data="con_features"),
        telebot.types.InlineKeyboardButton("🎁 Bonusy", callback_data="con_bonuses"),
        telebot.types.InlineKeyboardButton("⏰ Aktsii", callback_data="con_triggers"),
        telebot.types.InlineKeyboardButton("✨ Sozdat kartochku", callback_data="con_generate")
    )
    
    # Инициализируем данные конструктора
    user_states[user_id] = {
        'mode': 'constructor',
        'title': '',
        'features': [],
        'bonuses': [],
        'triggers': []
    }
    
    bot.send_message(
        chat_id,
        "🛠️ <b>Konstruktor kartochki</b>\n\n"
        "Vyberite elementy dlya dobavleniya.\n"
        "Kogda vse gotovo — nazhmite 'Sozdat kartochku'.",
        parse_mode="HTML",
        reply_markup=markup
    )

# === ТЕКСТОВЫЕ КНОПКИ ===

@bot.message_handler(func=lambda m: m.text == "📸 Sozdat kartochku")
def btn_create(message):
    bot.send_message(message.chat.id, "Otpravte foto tovara")

@bot.message_handler(func=lambda m: m.text == "❓ Pomoshch")
def btn_help(message):
    bot.send_message(
        message.chat.id,
        "Otpravte foto → vyberite stil → vyberite rezhim (AI/konstruktor/avto)"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Admin-panel")
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
    return "Bot rabotaet"

@app.route('/health')
def health():
    return {'status': 'ok'}, 200

# === ЗАПУСК ===

if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
    
    print("Bot zapuschen!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
