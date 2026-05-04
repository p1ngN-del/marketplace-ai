import os
import sys
import json
import base64
import sqlite3
from datetime import datetime
import io
import urllib.request

import telebot
from telebot import types
from flask import Flask, request
from huggingface_hub import InferenceClient
import dashscope
from dashscope import MultiModalConversation

# --- Pillow для дизайна ---
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT UNIQUE, username TEXT, first_name TEXT, last_name TEXT, first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, total_requests INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def log_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),))
    if c.fetchone():
        c.execute("UPDATE users SET username=?, first_name=?, last_name=?, last_seen=CURRENT_TIMESTAMP WHERE user_id=?", (username, first_name, last_name, str(user_id)))
    else:
        c.execute("INSERT INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)", (str(user_id), username, first_name, last_name))
    c.execute("INSERT INTO requests (user_id) VALUES (?)", (str(user_id),))
    c.execute("UPDATE users SET total_requests = total_requests + 1, last_seen = CURRENT_TIMESTAMP WHERE user_id = ?", (str(user_id),))
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

# Функции-заглушки для Pillow, если шрифт не загрузится
try:
    FONT_PATH = "font.ttf"
    font_title = ImageFont.truetype(FONT_PATH, 80)
    font_text = ImageFont.truetype(FONT_PATH, 40)
except:
    font_title = ImageFont.load_default()
    font_text = ImageFont.load_default()

# --- ФУНКЦИИ ОБРАБОТКИ ФОТО ---
def retouch_photo(product_bytes):
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"
        prompt = "Удали лишние объекты с фотографии (руки, провода, блики от лампы). Оставь только сам товар. Помести его на нейтральный, чистый, студийный белый фон."
        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]
        response = MultiModalConversation.call(api_key=DASHSCOPE_API_KEY, model="qwen-image-edit-plus", messages=messages, n=1, watermark=False, size="1024*1536")
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
    except:
        return None
    return None

def create_card(product_url):
    try:
        prompt = "Создай премиальную карточку товара для Wildberries. Идеальный студийный фон, мягкий свет, товар в центре. НИКАКОГО текста."
        messages = [{"role": "user", "content": [{"image": product_url}, {"text": prompt}]}]
        response = MultiModalConversation.call(api_key=DASHSCOPE_API_KEY, model="qwen-image-2.0-pro", messages=messages, n=1, watermark=False, size="1024*1536")
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
    except:
        return None
    return None

# --- ФУНКЦИЯ ДЛЯ НАЛОЖЕНИЯ ТЕКСТА (PILLOW - НОВЫЙ ДИЗАЙН) ---
def add_text_overlay(image_url, title, utp, cta):
    try:
        with urllib.request.urlopen(image_url) as f:
            img = Image.open(io.BytesIO(f.read())).convert("RGBA")
        overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        # --- Плашка для названия (сверху по центру) ---
        panel_w, panel_h = 800, 100
        x1 = (img.width - panel_w) // 2
        y1 = 30
        for i in range(panel_h):
            alpha = int(180 - (i * 0.6))
            draw.rectangle([x1, y1 + i, x1 + panel_w, y1 + i + 1], fill=(0, 0, 0, max(0, alpha)))
        draw.text((img.width//2 + 3, y1 + 33), title.upper(), font=font_title, fill=(0, 0, 0, 200), anchor="mt")
        draw.text((img.width//2, y1 + 30), title.upper(), font=font_title, fill=(255, 255, 255, 255), anchor="mt")

        # --- Плашка для УТП (слева по центру) ---
        utp_panel_w, utp_panel_h = 420, 120
        utp_x1, utp_y1 = 50, img.height // 2 - 60
        draw.rounded_rectangle([utp_x1, utp_y1, utp_x1 + utp_panel_w, utp_y1 + utp_panel_h], radius=15, fill=(255, 215, 0, 220))
        draw.text((utp_x1 + 23, utp_y1 + 33), utp, font=font_text, fill=(0, 0, 0, 200))
        draw.text((utp_x1 + 20, utp_y1 + 30), utp, font=font_text, fill=(0, 0, 0, 255))

        # --- Кнопка призыва к действию (справа внизу) ---
        btn_w, btn_h = 300, 70
        btn_x1, btn_y1 = img.width - btn_w - 50, img.height - btn_h - 50
        draw.rounded_rectangle([btn_x1, btn_y1, btn_x1 + btn_w, btn_y1 + btn_h], radius=35, fill=(0, 122, 255, 220))
        draw.text((btn_x1 + btn_w//2 + 3, btn_y1 + btn_h//2 + 3), cta, font=font_text, fill=(0, 0, 0, 200), anchor="mm")
        draw.text((btn_x1 + btn_w//2, btn_y1 + btn_h//2), cta, font=font_text, fill=(255, 255, 255, 255), anchor="mm")

        # Накладываем слой
        img = Image.alpha_composite(img, overlay)
        output = io.BytesIO()
        img.save(output, format='PNG')
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
    bot.send_message(message.chat.id, "👋 Привет! Отправьте мне фото товара, и я сразу задам несколько вопросов для создания красивой карточки.")

@bot.message_handler(commands=['admin'])
def admin_stats(message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    
    total_users, total_requests, recent_users = get_stats()
    
    text = f"📊 <b>Админ-панель</b>\n\n"
    text += f"👥 Всего пользователей: <b>{total_users}</b>\n"
    text += f"📸 Всего запросов: <b>{total_requests}</b>\n\n"
    text += "📋 <b>Последние 10 пользователей:</b>\n"
    
    for i, u in enumerate(recent_users, 1):
        name = u[3] if u[3] else "—"
        username = f"@{u[2]}" if u[2] else "—"
        text += f"{i}. {name} ({username}) — {u[7]} запросов\n"
    
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# --- НОВЫЙ ПРОСТОЙ ДИАЛОГ ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    # Сохраняем фото во временное хранилище
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    # Сохраняем фото для этого пользователя
    if not hasattr(bot, 'user_photos'):
        bot.user_photos = {}
    bot.user_photos[message.chat.id] = downloaded_file
    
    # Задаём первый вопрос
    msg = bot.reply_to(message, "📝 Введите **название товара**:")
    bot.register_next_step_handler(msg, process_title)

def process_title(message):
    if not hasattr(bot, 'user_data'):
        bot.user_data = {}
    bot.user_data[message.chat.id] = {'title': message.text}
    msg = bot.reply_to(message, "💎 Введите **главное преимущество (УТП)**:")
    bot.register_next_step_handler(msg, process_utp)

def process_utp(message):
    bot.user_data[message.chat.id]['utp'] = message.text
    msg = bot.reply_to(message, "🛒 Введите **призыв к действию** (например, 'Заказать сейчас'):")
    bot.register_next_step_handler(msg, process_cta)

def process_cta(message):
    bot.user_data[message.chat.id]['cta'] = message.text
    
    wait_msg = bot.reply_to(message, "⏳ Создаю карточку... (около 30-40 секунд)")
    
    try:
        # Берём сохранённое фото
        if not hasattr(bot, 'user_photos') or message.chat.id not in bot.user_photos:
            bot.send_message(message.chat.id, "❌ Фото не найдено. Отправьте его ещё раз.")
            return
        
        downloaded_file = bot.user_photos[message.chat.id]
        
        # Ретушь и генерация фона
        retouched_url = retouch_photo(downloaded_file)
        if not retouched_url:
            bot.edit_message_text("❌ Не удалось обработать фото.", message.chat.id, wait_msg.message_id)
            return
        
        card_url = create_card(retouched_url)
        if not card_url:
            bot.edit_message_text("❌ Не удалось создать фон.", message.chat.id, wait_msg.message_id)
            return

        # Берём сохраненный текст
        data = bot.user_data[message.chat.id]
        title = data.get('title', 'Товар')
        utp = data.get('utp', 'Премиум качество')
        cta = data.get('cta', 'Заказать сейчас')
        
        # Накладываем текст
        final_card = add_text_overlay(card_url, title, utp, cta)
        
        if final_card:
            bot.send_photo(message.chat.id, final_card, caption="✅ Готовая карточка с вашим текстом!")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось наложить текст.")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    finally:
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

# --- WEBHOOK ---
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
    return "Бот работает!"

if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"Webhook установлен: https://{railway_url}/webhook")
    print("Бот запущен...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
