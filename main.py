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
        telebot.types.BotCommand("start", "🚀 Запустить бота"),
        telebot.types.BotCommand("admin", "📊 Админ-панель"),
    ]
    try:
        bot.set_my_commands(commands)
        print("✅ Команды меню установлены")
    except Exception as e:
        print(f"⚠️ Не удалось установить команды: {e}")

set_bot_commands()

# Временное хранилище
user_cards = {}
user_analysis = {}  # Храним анализ товара для каждого пользователя

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

def analyze_product(image_url):
    """Глубокий анализ товара через AI — для генерации умных вопросов"""
    try:
        prompt = """Проанализируй этот товар на фото. Определи:
1. Что это за товар (категория, тип)
2. Какие 3-5 ключевых характеристики важны для покупателей этого товара
3. Какие боли/потребности решает этот товар
4. Кто целевая аудитория (возраст, пол, интересы)
5. Какие акценты в описании обычно работают лучше всего для таких товаров

Ответь строго в формате JSON:
{
  "category": "название категории",
  "product_name": "предполагаемое название",
  "key_features": ["характеристика 1", "характеристика 2", "характеристика 3"],
  "pain_points": ["боль 1", "боль 2"],
  "target_audience": "описание ЦА",
  "best_accents": ["акцент 1", "акцент 2", "акцент 3"],
  "suggested_questions": ["вопрос 1", "вопрос 2", "вопрос 3"]
}"""
        
        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]
        
        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-vl-max",
            messages=messages
        )
        
        if response.status_code == 200:
            text = response.output.choices[0].message.content[0]['text']
            # Пытаемся найти JSON в ответе
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return None
    except Exception as e:
        print(f"Ошибка анализа товара: {e}")
        return None
    return None

def get_font(size, weight='regular'):
    """Умная загрузка шрифтов с поддержкой кириллицы"""
    custom_fonts = {
        'bold': ['/app/Font bold.ttf', 'Font bold.ttf', '/app/font_bold.ttf', 'font_bold.ttf'],
        'regular': ['/app/Font regular.ttf', 'Font regular.ttf', '/app/font_regular.ttf', 'font_regular.ttf', '/app/font.ttf', 'font.ttf']
    }
    
    font_list = custom_fonts.get(weight, custom_fonts['regular'])
    
    for font_path in font_list:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size)
                test_text = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                bbox = font.getbbox(test_text)
                if bbox and (bbox[2] - bbox[0]) > 0:
                    return font
            except:
                continue
    
    system_fonts = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    
    for font_path in system_fonts:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except:
                continue
    
    return ImageFont.load_default()

def get_dominant_colors(img, num_colors=3):
    """Определяет доминирующие цвета изображения для адаптивного дизайна"""
    # Уменьшаем для скорости
    small = img.copy()
    small.thumbnail((150, 150))
    
    # Получаем пиксели
    pixels = list(small.getdata())
    if img.mode == 'RGBA':
        pixels = [(r, g, b) for r, g, b, a in pixels if a > 128]
    elif img.mode == 'RGB':
        pixels = list(pixels)
    else:
        pixels = [(r, g, b) for r, g, b in pixels]
    
    if not pixels:
        return [(128, 128, 128), (255, 255, 255), (0, 0, 0)]
    
    # Простая кластеризация по яркости
    pixels.sort(key=lambda x: sum(x) / 3)
    n = len(pixels)
    
    dark = pixels[n // 6] if n > 6 else pixels[0]
    mid = pixels[n // 2] if n > 2 else pixels[0]
    light = pixels[-n // 6] if n > 6 else pixels[-1]
    
    return [dark, mid, light]

def choose_text_colors(dominant_colors, bg_brightness):
    """Выбирает цвета текста на основе цветовой гаммы карточки"""
    dark, mid, light = dominant_colors
    
    if bg_brightness > 200:  # Очень светлый фон
        return {
            'primary': (30, 30, 30, 255),  # Почти чёрный
            'accent': (dark[0], dark[1], dark[2], 255) if sum(dark) < 400 else (80, 80, 80, 255),
            'secondary': (100, 100, 100, 255),
            'shadow': (255, 255, 255, 80),
            'overlay': (255, 255, 255, 30),
            'capsule_bg': (dark[0], dark[1], dark[2], 230) if sum(dark) < 400 else (50, 50, 50, 230),
            'capsule_text': (255, 255, 255, 255),
        }
    elif bg_brightness > 120:  # Средний фон
        return {
            'primary': (255, 255, 255, 255),
            'accent': (255, 200, 100, 255),  # Золотой
            'secondary': (220, 220, 220, 255),
            'shadow': (0, 0, 0, 100),
            'overlay': (0, 0, 0, 60),
            'capsule_bg': (255, 100, 80, 230),  # Коралловый
            'capsule_text': (255, 255, 255, 255),
        }
    else:  # Тёмный фон
        return {
            'primary': (255, 255, 255, 255),
            'accent': (255, 220, 100, 255),  # Ярко-золотой
            'secondary': (200, 200, 200, 255),
            'shadow': (0, 0, 0, 120),
            'overlay': (0, 0, 0, 40),
            'capsule_bg': (255, 255, 255, 230),  # Белая капсула на тёмном
            'capsule_text': (30, 30, 30, 255),
        }

def add_premium_text_to_image(image_url, title, subtitle=""):
    """Профессиональное наложение текста с адаптивным дизайном"""
    try:
        # Загрузка изображения
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
        
        # Анализ цветовой гаммы
        dominant_colors = get_dominant_colors(img)
        avg_brightness = sum(sum(c[:3]) / 3 for c in dominant_colors) / 3
        
        colors = choose_text_colors(dominant_colors, avg_brightness)
        
        # Создаём слои
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # --- АДАПТИВНЫЙ РАЗМЕР ШРИФТА ---
        def fit_text(text, max_width_ratio, base_size_ratio, weight='bold'):
            """Подбирает размер шрифта, чтобы текст влезал"""
            max_width = int(width * max_width_ratio)
            font_size = int(height * base_size_ratio)
            
            while font_size > int(height * 0.025):
                font = get_font(font_size, weight)
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                
                if text_width <= max_width:
                    return font, font_size, text_width
                
                font_size -= 1
            
            return get_font(int(height * 0.025), weight), int(height * 0.025), 0
        
        # --- ПОДГОТОВКА ЗАГОЛОВКА ---
        main_title = title.upper().strip()
        
        # Умный перенос длинных заголовков
        max_title_width = int(width * 0.85)
        words = main_title.split()
        lines = []
        current_line = ""
        
        for word in words:
            test = current_line + " " + word if current_line else word
            font, _, _ = fit_text(test, 0.85, 0.08)
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] <= max_title_width:
                current_line = test
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        
        if current_line:
            lines.append(current_line)
        
        wrapped_title = "\n".join(lines) if lines else main_title
        
        # --- МЕТКА "PREMIUM" ---
        label_text = "PREMIUM"
        label_font, label_size, _ = fit_text(label_text, 0.3, 0.035, 'regular')
        
        bbox_label = draw.textbbox((0, 0), label_text, font=label_font)
        label_width = bbox_label[2] - bbox_label[0]
        label_x = (width - label_width) // 2
        label_y = int(height * 0.04)
        
        # Линии вокруг PREMIUM
        line_len = int(width * 0.08)
        line_y = label_y + label_size // 2
        draw.line([(label_x - line_len - 10, line_y), (label_x - 10, line_y)], 
                  fill=colors['accent'], width=max(1, int(height * 0.003)))
        draw.line([(label_x + label_width + 10, line_y), (label_x + label_width + line_len + 10, line_y)], 
                  fill=colors['accent'], width=max(1, int(height * 0.003)))
        
        draw.text((label_x, label_y), label_text, font=label_font, fill=colors['accent'])
        
        # --- ОСНОВНОЙ ЗАГОЛОВОК ---
        title_font, title_size, title_width = fit_text(wrapped_title, 0.85, 0.075, 'bold')
        
        # Пересчитываем с учётом переноса
        bbox_title = draw.multiline_textbbox((0, 0), wrapped_title, font=title_font, spacing=4)
        title_width = bbox_title[2] - bbox_title[0]
        title_height = bbox_title[3] - bbox_title[1]
        
        title_x = (width - title_width) // 2
        title_y = label_y + int(height * 0.06)
        
        # Полупрозрачная подложка под заголовок
        padding_x = int(width * 0.04)
        padding_y = int(height * 0.02)
        bg_left = max(0, title_x - padding_x)
        bg_top = max(0, title_y - padding_y)
        bg_right = min(width, title_x + title_width + padding_x)
        bg_bottom = min(height, title_y + title_height + padding_y)
        
        draw.rounded_rectangle(
            [bg_left, bg_top, bg_right, bg_bottom],
            radius=int(height * 0.015),
            fill=colors['overlay']
        )
        
        # Тень заголовка
        shadow_offset = max(2, int(height * 0.004))
        draw.multiline_text(
            (title_x + shadow_offset, title_y + shadow_offset),
            wrapped_title,
            font=title_font,
            fill=colors['shadow'],
            align='center',
            spacing=4
        )
        
        # Основной заголовок
        draw.multiline_text(
            (title_x, title_y),
            wrapped_title,
            font=title_font,
            fill=colors['primary'],
            align='center',
            spacing=4
        )
        
        # --- КАПСУЛА CTA ---
        cta_text = subtitle if subtitle else "🔥 ХИТ ПРОДАЖ"
        cta_font, cta_size, cta_width = fit_text(cta_text, 0.8, 0.04, 'medium')
        
        # Если капсула слишком широкая — уменьшаем ещё
        while cta_width > int(width * 0.75) and cta_size > int(height * 0.02):
            cta_size -= 1
            cta_font = get_font(cta_size, 'medium')
            bbox = draw.textbbox((0, 0), cta_text, font=cta_font)
            cta_width = bbox[2] - bbox[0]
        
        bbox_cta = draw.textbbox((0, 0), cta_text, font=cta_font)
        cta_width = bbox_cta[2] - bbox_cta[0]
        cta_height = bbox_cta[3] - bbox_cta[1]
        
        pad_x = int(height * 0.025)
        pad_y = int(height * 0.015)
        capsule_w = cta_width + pad_x * 2
        capsule_h = cta_height + pad_y * 2
        
        capsule_x = (width - capsule_w) // 2
        capsule_y = title_y + title_height + int(height * 0.04)
        
        # Проверяем, не выходит ли капсула за низ изображения
        if capsule_y + capsule_h > height - int(height * 0.02):
            capsule_y = height - capsule_h - int(height * 0.02)
        
        # Градиентная капсула (имитация через полупрозрачность)
        draw.rounded_rectangle(
            [capsule_x, capsule_y, capsule_x + capsule_w, capsule_y + capsule_h],
            radius=int(height * 0.02),
            fill=colors['capsule_bg']
        )
        
        # Обводка капсулы
        draw.rounded_rectangle(
            [capsule_x + 1, capsule_y + 1, capsule_x + capsule_w - 1, capsule_y + capsule_h - 1],
            radius=int(height * 0.02),
            outline=colors['accent'],
            width=max(1, int(height * 0.002))
        )
        
        # Текст в капсуле
        draw.text(
            (capsule_x + pad_x, capsule_y + pad_y),
            cta_text,
            font=cta_font,
            fill=colors['capsule_text']
        )
        
        # --- ФИНАЛЬНОЕ ОБЪЕДИНЕНИЕ ---
        final_img = Image.alpha_composite(img, overlay)
        final_img = final_img.convert('RGB')
        
        # Улучшение резкости
        enhancer = ImageEnhance.Sharpness(final_img)
        final_img = enhancer.enhance(1.2)
        
        output = BytesIO()
        final_img.save(output, format='JPEG', quality=95)
        output.seek(0)
        
        return output
        
    except Exception as e:
        print(f"❌ Ошибка наложения текста: {e}")
        import traceback
        traceback.print_exc()
        return None

# --- ОБРАБОТЧИКИ КОМАНД ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    is_admin = user_id == str(ADMIN_ID)
    
    welcome_text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 Отправьте фото товара — я:\n"
        "✨ Уберу лишние объекты\n"
        "🎨 Создам студийный фон\n"
        "📝 Добавлю дизайнерский текст\n\n"
        "Просто отправьте фото!"
    )
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        telebot.types.KeyboardButton("📸 Создать карточку"),
        telebot.types.KeyboardButton("❓ Помощь"),
    ]
    if is_admin:
        buttons.append(telebot.types.KeyboardButton("📊 Админ-панель"))
    
    markup.add(*buttons)
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML", reply_markup=markup)

@bot.message_handler(commands=['admin'])
def admin_stats(message):
    user_id = str(message.from_user.id)
    if user_id != str(ADMIN_ID):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа.")
        return
    
    total_users, total_requests, recent_users = get_stats()
    
    text = f"📊 <b>Админ-панель</b>\n\n"
    text += f"👥 Всего пользователей: <b>{total_users}</b>\n"
    text += f"📸 Всего запросов: <b>{total_requests}</b>\n\n"
    text += "📋 <b>Последние 10 пользователей:</b>\n"
    
    for i, u in enumerate(recent_users, 1):
        name = u[3] if u[3] else "—"
        username = f"@{u[2]}" if u[2] else "—"
        requests_count = u[7] if len(u) > 7 else 0
        text += f"{i}. {name} ({username}) — {requests_count} запросов\n"
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh"))
    markup.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="admin_close"))
    
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)

# --- ОБРАБОТЧИК ФОТО (С УМНЫМ АНАЛИЗОМ) ---

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    wait_msg = bot.reply_to(message, "⏳ Анализирую товар и создаю карточку...\n\n🔄 Этап 1/3: Ретушь...")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Этап 1: Ретушь
        retouched_url = retouch_photo(downloaded_file)
        if not retouched_url:
            bot.edit_message_text("❌ Не удалось обработать фото.", message.chat.id, wait_msg.message_id)
            return
        
        bot.edit_message_text(
            "⏳ Анализирую товар...\n\n✅ Ретушь завершена\n🔄 Этап 2/3: Создание карточки...", 
            message.chat.id, wait_msg.message_id
        )
        
        # Этап 2: Карточка
        card_url = create_card(retouched_url)
        if not card_url:
            bot.edit_message_text("❌ Не удалось создать карточку.", message.chat.id, wait_msg.message_id)
            return
        
        user_cards[user_id] = card_url
        
        bot.edit_message_text(
            "⏳ Анализирую товар...\n\n✅ Ретушь\n✅ Карточка\n🔄 Этап 3/3: AI-анализ товара...", 
            message.chat.id, wait_msg.message_id
        )
        
        # Этап 3: Глубокий анализ товара
        analysis = analyze_product(card_url)
        user_analysis[user_id] = analysis
        
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass
        
        # Формируем сообщение с результатами анализа
        if analysis:
            analysis_text = (
                f"✅ <b>Карточка готова!</b>\n\n"
                f"🔍 <b>AI проанализировал товар:</b>\n"
                f"• Категория: {analysis.get('category', '—')}\n"
                f"• Целевая аудитория: {analysis.get('target_audience', '—')}\n\n"
                f"💡 <b>Рекомендуемые акценты:</b>\n"
            )
            for accent in analysis.get('best_accents', [])[:3]:
                analysis_text += f"  • {accent}\n"
            
            analysis_text += "\n❓ <b>Хотите, чтобы я задал уточняющие вопросы для идеального оформления?</b>"
            
            # Кнопки выбора
            markup = telebot.types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                telebot.types.InlineKeyboardButton("🤖 AI-вопросы (рекомендуется)", callback_data="ai_questions"),
                telebot.types.InlineKeyboardButton("✨ Авто-текст", callback_data="auto_text"),
                telebot.types.InlineKeyboardButton("✍️ Свой текст", callback_data="custom_text"),
                telebot.types.InlineKeyboardButton("🚫 Без текста", callback_data="no_text")
            )
            
            bot.send_photo(message.chat.id, card_url, caption=analysis_text, reply_markup=markup, parse_mode="HTML")
        else:
            # Если анализ не сработал — стандартные кнопки
            markup = telebot.types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                telebot.types.InlineKeyboardButton("✨ Автоматический текст (AI)", callback_data="auto_text"),
                telebot.types.InlineKeyboardButton("✍️ Ввести свой текст", callback_data="custom_text"),
                telebot.types.InlineKeyboardButton("🚫 Без текста", callback_data="no_text")
            )
            bot.send_photo(message.chat.id, card_url, caption="✅ <b>Карточка готова!</b>\n\nВыберите вариант:", reply_markup=markup, parse_mode="HTML")
        
    except Exception as e:
        print(f"Ошибка обработки: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте ещё раз.")
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

# --- ОБРАБОТЧИКИ CALLBACK ---

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = str(call.from_user.id)
    
    # --- AI ВОПРОСЫ ---
    if call.data == "ai_questions":
        analysis = user_analysis.get(user_id)
        if not analysis:
            bot.answer_callback_query(call.id, "❌ Анализ не найден")
            return
        
        questions = analysis.get('suggested_questions', [
            "Какое главное преимущество вашего товара?",
            "Какая цена у товара?",
            "Есть ли у вас акция или скидка?"
        ])
        
        # Берём первый вопрос
        first_question = questions[0] if questions else "Какое главное преимущество вашего товара?"
        user_analysis[user_id]['current_question'] = 0
        user_analysis[user_id]['questions'] = questions
        user_analysis[user_id]['answers'] = []
        
        bot.answer_callback_query(call.id, "🤖 Задаю вопросы...")
        
        msg = bot.send_message(
            call.message.chat.id,
            f"🤖 <b>Вопрос 1 из {len(questions)}:</b>\n\n{first_question}\n\n"
            f"Ответьте текстом, и я подберу идеальный дизайн карточки.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_ai_question)
        return
    
    # --- АДМИН КНОПКИ ---
    elif call.data == "admin_refresh":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Нет доступа")
            return
        bot.answer_callback_query(call.id, "🔄 Обновляю...")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        class FakeMessage:
            def __init__(self, chat, from_user):
                self.chat = chat
                self.from_user = from_user
        fake_msg = FakeMessage(call.message.chat, call.from_user)
        admin_stats(fake_msg)
        return
    
    elif call.data == "admin_close":
        if user_id != str(ADMIN_ID):
            bot.answer_callback_query(call.id, "⛔ Нет доступа")
            return
        bot.answer_callback_query(call.id, "✅ Закрыто")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return
    
    # --- СТАНДАРТНЫЕ КНОПКИ ---
    elif call.data == "auto_text":
        card_url = user_cards.get(user_id)
        if not card_url:
            bot.answer_callback_query(call.id, "❌ Карточка не найдена")
            return
        
        bot.answer_callback_query(call.id, "⏳ Генерирую...")
        wait_msg = bot.send_message(call.message.chat.id, "⏳ Генерирую AI-описание...")
        
        description = generate_product_description(card_url)
        if not description:
            description = "Премиум товар"
        
        bot.edit_message_text(
            f"⏳ Накладываю текст: <b>{description}</b>...", 
            call.message.chat.id, wait_msg.message_id, parse_mode="HTML"
        )
        
        final_image = add_premium_text_to_image(card_url, description)
        
        try:
            bot.delete_message(call.message.chat.id, wait_msg.message_id)
        except:
            pass
        
        if final_image:
            bot.send_photo(
                call.message.chat.id, final_image,
                caption=f"✅ <b>Готово!</b>\n\n📝 {description}",
                parse_mode="HTML"
            )
        else:
            bot.send_photo(call.message.chat.id, card_url, caption="❌ Не удалось наложить текст")
    
    elif call.data == "custom_text":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            "✍️ <b>Введите текст для карточки:</b>\n\n"
            "Рекомендуется 2-5 слов.\n"
            "Примеры: 'ЛЕТНЯЯ РАСПРОДАЖА', 'НОВИНКА 2026', 'ХИТ ПРОДАЖ'",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_custom_text)
    
    elif call.data == "no_text":
        card_url = user_cards.get(user_id)
        if card_url:
            bot.answer_callback_query(call.id, "✅ Отправляю")
            bot.send_photo(call.message.chat.id, card_url, caption="✅ <b>Карточка без текста</b>", parse_mode="HTML")
        else:
            bot.answer_callback_query(call.id, "❌ Карточка не найдена")

def process_ai_question(message):
    """Обработка ответов на AI-вопросы"""
    user_id = str(message.from_user.id)
    analysis = user_analysis.get(user_id)
    
    if not analysis:
        bot.send_message(message.chat.id, "❌ Сессия истекла. Отправьте фото заново.")
        return
    
    # Сохраняем ответ
    if 'answers' not in analysis:
        analysis['answers'] = []
    analysis['answers'].append(message.text.strip())
    
    current = analysis.get('current_question', 0)
    questions = analysis.get('questions', [])
    
    # Следующий вопрос или финал
    if current + 1 < len(questions):
        analysis['current_question'] = current + 1
        next_q = questions[current + 1]
        
        msg = bot.send_message(
            message.chat.id,
            f"🤖 <b>Вопрос {current + 2} из {len(questions)}:</b>\n\n{next_q}",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_ai_question)
    else:
        # Все ответы получены — генерируем финальный текст
        answers = analysis['answers']
        category = analysis.get('category', 'товар')
        
        # Формируем текст на основе ответов
        if len(answers) >= 2:
            title = f"{answers[0].upper()}"
            subtitle = answers[1] if len(answers) > 1 else "🔥 ХИТ ПРОДАЖ"
        else:
            title = answers[0].upper() if answers else "ПРЕМИУМ ТОВАР"
            subtitle = "🔥 ХИТ ПРОДАЖ"
        
        # Ограничиваем длину
        title = title[:40]
        subtitle = subtitle[:30]
        
        bot.send_message(message.chat.id, f"⏳ Создаю дизайн на основе ваших ответов...")
        
        card_url = user_cards.get(user_id)
        if card_url:
            final_image = add_premium_text_to_image(card_url, title, subtitle)
            if final_image:
                bot.send_photo(
                    message.chat.id, final_image,
                    caption=f"✅ <b>Готово!</b>\n\nНа основе ваших ответов:\n📝 <b>{title}</b>\n💬 {subtitle}",
                    parse_mode="HTML"
                )
            else:
                bot.send_message(message.chat.id, "❌ Ошибка создания дизайна")

def process_custom_text(message):
    """Обработка пользовательского текста"""
    user_id = str(message.from_user.id)
    card_url = user_cards.get(user_id)
    
    if not card_url:
        bot.send_message(message.chat.id, "❌ Карточка не найдена.")
        return
    
    custom_text = message.text.strip().upper()
    if len(custom_text) > 100:
        bot.send_message(message.chat.id, "⚠️ Текст слишком длинный.")
        return
    
    wait_msg = bot.send_message(message.chat.id, f"⏳ Накладываю: <b>{custom_text}</b>", parse_mode="HTML")
    
    final_image = add_premium_text_to_image(card_url, custom_text)
    
    try:
        bot.delete_message(message.chat.id, wait_msg.message_id)
    except:
        pass
    
    if final_image:
        bot.send_photo(
            message.chat.id, final_image,
            caption=f"✅ <b>Готово!</b>\n\n📝 {custom_text}",
            parse_mode="HTML"
        )
    else:
        bot.send_message(message.chat.id, "❌ Не удалось наложить текст.")

# --- ТЕКСТОВЫЕ КНОПКИ ---

@bot.message_handler(func=lambda message: message.text == "📸 Создать карточку")
def button_create(message):
    bot.send_message(
        message.chat.id,
        "📸 <b>Отправьте фото товара</b>\n\nЯ создам профессиональную карточку.",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda message: message.text == "❓ Помощь")
def button_help(message):
    help_text = (
        "❓ <b>Помощь</b>\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Отправьте фото товара\n"
        "2. Дождитесь обработки\n"
        "3. Выберите вариант текста:\n"
        "   • 🤖 AI-вопросы — бот задаст вопросы и сделает идеальный дизайн\n"
        "   • ✨ Авто-текст — AI сам придумает текст\n"
        "   • ✍️ Свой текст — вы пишете сами\n"
        "   • 🚫 Без текста — только фото\n\n"
        "<b>Команды:</b>\n"
        "/start — Перезапуск\n"
        "/admin — Админ-панель"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(func=lambda message: message.text == "📊 Админ-панель")
def button_admin(message):
    admin_stats(message)

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
    return "🤖 Бот работает! AI-дизайн карточек товаров"

@app.route('/health')
def health():
    return {'status': 'ok', 'users': len(user_cards)}, 200

# --- ЗАПУСК ---

if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"✅ Webhook: https://{railway_url}/webhook")
    
    print("🚀 Бот запущен с AI-анализом товаров!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
