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

# --- ИНФОГРАФИКА (ПОЛУПРОЗРАЧНЫЕ ПЛАШКИ) ---
# --- ИНФОГРАФИКА (ПОЛУПРОЗРАЧНЫЕ ПЛАШКИ С ОДНОЙ СТРОКОЙ) ---
def add_infographic(base_image, title, features=None, style_key="clean_white"):
    try:
        style = BG_STYLES.get(style_key, BG_STYLES["clean_white"])
        width, height = base_image.size
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        r, g, b = style['text_color']
        plate_fill = (r, g, b, 15)      # <-- ПРАКТИЧЕСКИ НЕВИДИМАЯ
        plate_outline = (r, g, b, 10)   # <-- ПРАКТИЧЕСКИ НЕВИДИМАЯ
        text_fill = (r, g, b, 220)
        margin = int(width * 0.05)
        plate_radius = int(height * 0.02)
        
        # --- ЗАГОЛОВОК КАРТОЧКИ (НА КАЖДОЙ) ---
        if title:
            title_text = title.upper()[:40]
            title_font = get_font(int(height * 0.04), 'regular')
            tw = draw.textbbox((0, 0), title_text, font=title_font)[2]
            draw.text(((width - tw) // 2, int(height * 0.03)), title_text, font=title_font, fill=text_fill)
        
        # --- ХАРАКТЕРИСТИКИ (ПЛАШКИ ПО БОКАМ) ---
        if features and len(features) > 0:
            badge_w = int(width * 0.42)
            badge_h = int(height * 0.08)
            start_y = int(height * 0.22)
            gap = int(height * 0.02)
            
            for i, feat in enumerate(features[:4]):
                bx = margin if i % 2 == 0 else width - margin - badge_w
                by = start_y + (i // 2) * (badge_h + gap)
                
                # Плашка
                draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=plate_radius, fill=plate_fill, outline=plate_outline, width=1)
                
                # ОДНА СТРОКА: "Заголовок: значение"
                text = feat.get('text', '')
                if text:
                    font_size = int(height * 0.025)
                    font = get_font(font_size, 'regular')
                    while draw.textbbox((0, 0), text, font=font)[2] > badge_w - 20 and font_size > 10:
                        font_size -= 1
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

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ЗАМЕНИТЕ ИХ) ===
def question_to_label(question):
    if not question: return ""
    q = question.lower()
    if 'материал' in q: return 'Материал'
    if 'модел' in q or 'совместим' in q or 'подходит' in q: return 'Совместимость'
    if 'длина' in q or 'размер' in q: return 'Размер'
    if 'цвет' in q: return 'Цвет'
    if 'бонус' in q or 'подар' in q: return 'Бонус'
    if 'скидк' in q or 'акци' in q: return 'Акция'
    if 'лучше' in q or 'преимуществ' in q: return 'Преимущество'
    if 'гаранти' in q: return 'Гарантия'
    # Возвращаем пустую строку, если не можем определить
    return ""

def clean_answer(answer, question):
    ans = answer.strip()
    q = question.lower()
    
    # Если ответ отрицательный — возвращаем None (не показываем плашку)
    if ans.lower() in ["нет", "no", "нету", "отсутствует", "бонусов нет"]:
        return None
    if 'нет' in ans.lower() and len(ans) < 10:
        return None
    
    # Превращаем "Да, 10%" в "Скидка 10%"
    if 'акци' in q or 'скидк' in q:
        if ans.lower().startswith('да'):
            return ans.replace('Да,', 'Скидка').replace('да,', 'Скидка')
    
    # Превращаем "Да" в "Совместимо"
    if ('совместим' in q or 'подходит' in q) and ans.lower() == 'да':
        return 'Совместимо'
    
    return ans

def format_feature_text(label, value):
    """Формирует одну строку: 'Материал: натуральная кожа'"""
    if label:
        return f"{label}: {value}"
    return value

# === ОБНОВЛЁННАЯ finish_ai_mode ===
def finish_ai_mode(chat_id, user_id):
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    questions = analysis.get('questions', [])
    style_key = user_data[user_id]['style']
    
    # --- ЗАГОЛОВОК (НА КАЖДОЙ КАРТОЧКЕ) ---
    title = analysis.get('product_name', '').upper()
    if not title:
        title = "ПРЕМИУМ ТОВАР"
    
    # --- ГЕНЕРИРУЕМ ПЛАШКИ ---
    all_features = []
    for i, ans in enumerate(answers):
        if not ans: continue
        
        question = questions[i] if i < len(questions) else ""
        label = question_to_label(question)
        clean_value = clean_answer(ans, question)
        
        # Пропускаем если ответ отрицательный
        if clean_value is None:
            continue
        
        # Пропускаем если ответ бессмысленный
        if len(clean_value) <= 1:
            continue
        
        # Формируем одну строку
        text = format_feature_text(label, clean_value)
        
        all_features.append({"text": text[:40]})
    
    if not all_features:
        bot.send_message(chat_id, "❌ Недостаточно данных для карточек")
        return
    
    # --- РАСПРЕДЕЛЯЕМ ПО КАРТОЧКАМ ---
    cards = []
    image_urls = [
        user_data[user_id].get('base_card'),
        user_data[user_id].get('left_card', user_data[user_id].get('base_card')),
        user_data[user_id].get('right_card', user_data[user_id].get('base_card')),
    ]
    
    for i in range(0, len(all_features), 3):
        chunk = all_features[i:i+3]
        if not chunk: continue
        
        img_url = image_urls[i // 3] if i // 3 < len(image_urls) else image_urls[0]
        if not img_url: continue
        
        try:
            img = Image.open(BytesIO(requests.get(img_url).content)).convert('RGBA')
        except:
            continue
        
        # ЗАГОЛОВОК ПЕРЕДАЁТСЯ НА КАЖДУЮ КАРТОЧКУ
        card = add_infographic(img, title, chunk[:3], style_key)
        if card:
            cards.append(card)
    
    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/{len(cards)}")
    else:
        bot.send_message(chat_id, "❌ Ошибка при создании карточек.")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def question_to_label(question):
    if not question: return "Характеристика"
    q = question.lower()
    if 'материал' in q: return 'Материал'
    if 'модел' in q or 'совместим' in q or 'подходит' in q: return 'Совместимость'
    if 'длина' in q or 'размер' in q: return 'Размер'
    if 'цвет' in q: return 'Цвет'
    if 'бонус' in q or 'подар' in q: return 'Бонус'
    if 'скидк' in q or 'акци' in q: return 'Акция'
    if 'лучше' in q or 'преимуществ' in q: return 'Преимущество'
    if 'гаранти' in q: return 'Гарантия'
    words = question.split()[:3]
    return ' '.join(words)

def clean_answer(answer, question):
    ans = answer.strip()
    q = question.lower()
    if 'акци' in q or 'скидк' in q:
        if ans.lower().startswith('да'):
            return ans.replace('Да,', 'Скидка').replace('да,', 'Скидка')
    # Убираем "Да" если это ответ на вопрос о совместимости
    if ('совместим' in q or 'подходит' in q) and ans.lower() == 'да':
        return 'Совместимо'
    return ans

def get_icon_for_label(label):
    if 'Материал' in label: return '🔷'
    if 'Совместимость' in label: return '✅'
    if 'Размер' in label: return '📏'
    if 'Цвет' in label: return '🎨'
    if 'Бонус' in label: return '🎁'
    if 'Акция' in label: return '⏰'
    if 'Преимущество' in label: return '⭐'
    if 'Гарантия' in label: return '🛡️'
    return '📦'

def generate_auto(chat_id, user_id):
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    analysis = user_analysis.get(user_id, {})
    style_key = user_data[user_id]['style']
    
    title = analysis.get('product_name', 'ПРЕМИУМ ТОВАР').upper()
    features = [
        {"icon": "🛡️", "label": "Гарантия", "value": "12 месяцев"},
        {"icon": "🚚", "label": "Доставка", "value": "Бесплатно"},
        {"icon": "⭐", "label": "Качество", "value": "Премиум"},
    ]
    
    cards = []
    image_url = user_data[user_id].get('base_card')
    if image_url:
        try:
            img = Image.open(BytesIO(requests.get(image_url).content)).convert('RGBA')
            card = add_infographic(img, title, features, None, None, style_key)
            if card: cards.append(card)
        except: pass
    
    if cards:
        for i, card in enumerate(cards):
            bot.send_photo(chat_id, card, caption=f"✅ Карточка {i+1}/{len(cards)}")
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
