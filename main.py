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
            text = text.strip().replace('"', '').replace("'", "")
            return text[:50]
    except Exception as e:
        print(f"Ошибка генерации описания: {e}")
        return None
    return None
    
def get_font(size, weight='regular'):
    """
    Умная загрузка шрифтов с поддержкой кириллицы.
    Порядок приоритета: кастомный → системный с кириллицей → встроенный
    """
    
    # 1. Пробуем твои кастомные шрифты
    custom_fonts = {
        'bold': ['/app/Font bold.ttf', 'Font bold.ttf', '/app/font_bold.ttf', 'font_bold.ttf'],
        'regular': ['/app/Font regular.ttf', 'Font regular.ttf', '/app/font_regular.ttf', 'font_regular.ttf', '/app/font.ttf', 'font.ttf']
    }
    
    font_list = custom_fonts.get(weight, custom_fonts['regular'])
    
    for font_path in font_list:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size)
                # Проверяем, что шрифт реально поддерживает кириллицу
                test_text = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                bbox = font.getbbox(test_text)
                if bbox and (bbox[2] - bbox[0]) > 0:
                    print(f"✅ Загружен шрифт: {font_path}")
                    return font
            except Exception as e:
                print(f"⚠️ Не удалось загрузить {font_path}: {e}")
                continue
    
    # 2. Пробуем системные шрифты с гарантированной кириллицей
    system_fonts = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        'C:/Windows/Fonts/arial.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ]
    
    for font_path in system_fonts:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size)
                print(f"✅ Загружен системный шрифт: {font_path}")
                return font
            except:
                continue
    
    # 3. Последний fallback
    print("⚠️ Используется стандартный шрифт (кириллица может не работать)")
    return ImageFont.load_default()

def add_premium_text_to_image(image_url, title, subtitle=""):
    """
    Профессиональное наложение текста на изображение
    Адаптируется под цветовую гамму карточки
    """
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
        
        # --- АНАЛИЗ ЦВЕТОВОЙ ГАММЫ КАРТОЧКИ ---
        # Берём образцы пикселей из разных зон
        sample_points = [
            (width // 4, height // 4),
            (width * 3 // 4, height // 4),
            (width // 2, height // 2),
            (width // 4, height * 3 // 4),
            (width * 3 // 4, height * 3 // 4),
        ]
        
        total_brightness = 0
        for x, y in sample_points:
            r, g, b, a = img.getpixel((min(x, width-1), min(y, height-1)))
            brightness = (r * 0.299 + g * 0.587 + b * 0.114)
            total_brightness += brightness
        
        avg_brightness = total_brightness / len(sample_points)
        
        # Определяем цвета текста в зависимости от фона
        if avg_brightness > 180:  # Светлый фон
            text_color = (30, 30, 30, 255)  # Тёмно-серый
            accent_color = (200, 50, 50, 255)  # Красный акцент
            shadow_color = (255, 255, 255, 100)  # Белая тень
        elif avg_brightness > 100:  # Средний фон
            text_color = (255, 255, 255, 255)  # Белый
            accent_color = (255, 200, 50, 255)  # Золотой
            shadow_color = (0, 0, 0, 120)  # Тёмная тень
        else:  # Тёмный фон
            text_color = (255, 255, 255, 255)  # Белый
            accent_color = (255, 100, 100, 255)  # Ярко-красный
            shadow_color = (0, 0, 0, 150)  # Глубокая тень
        
        # Создание слоя для рисования
        overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # --- АДАПТИВНЫЙ РАЗМЕР ШРИФТА ---
        def get_adaptive_font(base_size, weight='bold'):
            """Пробуем разные размеры, пока текст не влезет"""
            for scale in [1.0, 0.85, 0.7, 0.55, 0.4]:
                size = int(height * base_size * scale)
                font = get_font(size, weight)
                return font, size  # Пока просто возвращаем, проверку ниже
            return get_font(int(height * base_size * 0.4), weight), int(height * base_size * 0.4)
        
        # --- ПОДГОТОВКА ТЕКСТА ---
        main_title = title.upper()
        
        # Умный перенос: сначала пробуем без переноса, потом с переносом
        max_width = int(width * 0.85)  # Максимальная ширина текста (85% от ширины карточки)
        
        # Подбираем размер заголовка
        title_font_size = int(height * 0.08)
        title_font = get_font(title_font_size, 'bold')
        
        # Проверяем, влезает ли текст
        bbox = draw.multiline_textbbox((0, 0), main_title, font=title_font)
        text_width = bbox[2] - bbox[0]
        
        # Уменьшаем шрифт, пока не влезет
        while text_width > max_width and title_font_size > int(height * 0.03):
            title_font_size -= 2
            title_font = get_font(title_font_size, 'bold')
            bbox = draw.multiline_textbbox((0, 0), main_title, font=title_font)
            text_width = bbox[2] - bbox[0]
        
        # Если всё ещё не влезает — переносим по словам
        if text_width > max_width:
            words = main_title.split()
            lines = []
            current_line = ""
            for word in words:
                test_line = current_line + " " + word if current_line else word
                bbox = draw.textbbox((0, 0), test_line, font=title_font)
                if bbox[2] - bbox[0] > max_width:
                    if current_line:
                        lines.append(current_line)
                        current_line = word
                    else:
                        # Слово слишком длинное, уменьшаем шрифт ещё
                        while True:
                            title_font_size -= 2
                            if title_font_size < int(height * 0.025):
                                break
                            title_font = get_font(title_font_size, 'bold')
                            bbox = draw.textbbox((0, 0), word, font=title_font)
                            if bbox[2] - bbox[0] <= max_width:
                                current_line = word
                                break
                else:
                    current_line = test_line
            if current_line:
                lines.append(current_line)
            wrapped_title = "\n".join(lines) if lines else main_title
        else:
            wrapped_title = main_title
        
        # --- ЗАГОЛОВОК "ПРЕМИУМ" (мелкий текст сверху) ---
        header_text = "ПРЕМИУМ"
        header_font_size = max(int(title_font_size * 0.4), int(height * 0.02))
        header_font = get_font(header_font_size, 'regular')
        
        bbox_header = draw.textbbox((0, 0), header_text, font=header_font)
        header_width = bbox_header[2] - bbox_header[0]
        header_x = (width - header_width) // 2
        header_y = int(height * 0.04)
        
        # Маленькая линия над заголовком
        line_width = int(header_width * 1.5)
        line_x1 = (width - line_width) // 2
        line_y = header_y - int(height * 0.015)
        draw.line([(line_x1, line_y), (line_x1 + line_width, line_y)], 
                  fill=accent_color, width=max(1, int(height * 0.003)))
        
        draw.text((header_x, header_y), header_text, font=header_font, fill=accent_color)
        
        # --- ОСНОВНОЙ ЗАГОЛОВОК ---
        bbox_title = draw.multiline_textbbox((0, 0), wrapped_title, font=title_font)
        title_width = bbox_title[2] - bbox_title[0]
        title_height = bbox_title[3] - bbox_title[1]
        title_x = (width - title_width) // 2
        title_y = header_y + int(height * 0.05)
        
        # Полупрозрачная подложка под заголовок для читаемости
        padding = int(height * 0.02)
        overlay_bg = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw_bg = ImageDraw.Draw(overlay_bg)
        
        bg_left = max(0, title_x - padding)
        bg_top = max(0, title_y - padding)
        bg_right = min(width, title_x + title_width + padding)
        bg_bottom = min(height, title_y + title_height + padding * 2)
        
        # Размытая подложка
        draw_bg.rounded_rectangle(
            [bg_left, bg_top, bg_right, bg_bottom],
            radius=int(height * 0.02),
            fill=(0, 0, 0, 60) if avg_brightness > 150 else (255, 255, 255, 40)
        )
        
        # Тень заголовка
        shadow_offset = max(2, int(height * 0.004))
        draw.multiline_text(
            (title_x + shadow_offset, title_y + shadow_offset),
            wrapped_title,
            font=title_font,
            fill=shadow_color,
            align='center',
            spacing=int(height * 0.008)
        )
        
        # Основной заголовок
        draw.multiline_text(
            (title_x, title_y),
            wrapped_title,
            font=title_font,
            fill=text_color,
            align='center',
            spacing=int(height * 0.008)
        )
        
        # --- КАПСУЛА С ПРИЗЫВОМ К ДЕЙСТВИЮ ---
        cta_text = subtitle if subtitle else "🔥 ХИТ ПРОДАЖ"
        
        # Подбираем размер шрифта для капсулы
        cta_font_size = int(height * 0.045)
        cta_font = get_font(cta_font_size, 'medium')
        
        bbox_cta = draw.textbbox((0, 0), cta_text, font=cta_font)
        cta_width = bbox_cta[2] - bbox_cta[0]
        
        # Уменьшаем, если не влезает
        while cta_width > int(width * 0.8) and cta_font_size > int(height * 0.025):
            cta_font_size -= 2
            cta_font = get_font(cta_font_size, 'medium')
            bbox_cta = draw.textbbox((0, 0), cta_text, font=cta_font)
            cta_width = bbox_cta[2] - bbox_cta[0]
        
        cta_height = bbox_cta[3] - bbox_cta[1]
        
        capsule_padding_x = int(height * 0.025)
        capsule_padding_y = int(height * 0.015)
        capsule_x = (width - cta_width - capsule_padding_x * 2) // 2
        capsule_y = title_y + title_height + int(height * 0.05)
        
        # Капсула с градиентом (имитация)
        draw.rounded_rectangle(
            [capsule_x, capsule_y, 
             capsule_x + cta_width + capsule_padding_x * 2, 
             capsule_y + cta_height + capsule_padding_y * 2],
            radius=int(height * 0.025),
            fill=accent_color
        )
        
        # Небольшая белая обводка капсулы
        draw.rounded_rectangle(
            [capsule_x + 1, capsule_y + 1, 
             capsule_x + cta_width + capsule_padding_x * 2 - 1, 
             capsule_y + cta_height + capsule_padding_y * 2 - 1],
            radius=int(height * 0.025),
            outline=(255, 255, 255, 80),
            width=1
        )
        
        # Текст в капсуле
        draw.text(
            (capsule_x + capsule_padding_x, capsule_y + capsule_padding_y),
            cta_text,
            font=cta_font,
            fill=(255, 255, 255, 255)
        )
        
        # --- ФИНАЛЬНОЕ ОБЪЕДИНЕНИЕ ---
        # Сначала подложка, потом основной overlay
        img = Image.alpha_composite(img, overlay_bg)
        final_img = Image.alpha_composite(img, overlay)
        final_img = final_img.convert('RGB')
        
        # Лёгкое повышение резкости
        final_img = final_img.filter(ImageFilter.SHARPEN)
        
        # Сохранение
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
    
    welcome_text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 Отправьте фото товара — я:\n"
        "✨ Уберу лишние объекты\n"
        "🎨 Создам студийный фон\n"
        "📝 Добавлю красивый дизайнерский текст\n\n"
        "Просто отправьте фото!"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML")

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

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = str(message.from_user.id)
    log_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    wait_msg = bot.reply_to(message, "⏳ Обрабатываю фото...\n\n🔄 Этап 1/3: Ретушь изображения...")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Этап 1: Ретушь
        retouched_url = retouch_photo(downloaded_file)
        if not retouched_url:
            bot.edit_message_text("❌ Не удалось обработать фото. Попробуйте другое изображение.", 
                                  message.chat.id, wait_msg.message_id)
            return
        
        bot.edit_message_text("⏳ Обрабатываю фото...\n\n✅ Этап 1/3: Ретушь завершена\n🔄 Этап 2/3: Создание карточки...", 
                              message.chat.id, wait_msg.message_id)
        
        # Этап 2: Генерация карточки
        card_url = create_card(retouched_url)
        if not card_url:
            bot.edit_message_text("❌ Не удалось создать карточку. Попробуйте ещё раз.", 
                                  message.chat.id, wait_msg.message_id)
            return
        
        # Сохраняем карточку для пользователя
        user_cards[user_id] = card_url
        
        bot.edit_message_text("⏳ Обрабатываю фото...\n\n✅ Этап 1/3: Ретушь завершена\n✅ Этап 2/3: Карточка создана\n🔄 Этап 3/3: Подготовка текста...", 
                              message.chat.id, wait_msg.message_id)
        
        # Удаляем сообщение о прогрессе
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass
        
        # Отправляем карточку с выбором варианта текста
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton("✨ Автоматический текст (AI)", callback_data="auto_text"),
            telebot.types.InlineKeyboardButton("✍️ Ввести свой текст", callback_data="custom_text"),
            telebot.types.InlineKeyboardButton("🚫 Без текста", callback_data="no_text")
        )
        
        bot.send_photo(
            message.chat.id, 
            card_url, 
            caption="✅ <b>Карточка готова!</b>\n\nВыберите вариант добавления текста:", 
            reply_markup=markup,
            parse_mode="HTML"
        )
        
    except Exception as e:
        print(f"Ошибка обработки: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при обработке. Попробуйте ещё раз.")
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

# --- ОБРАБОТЧИКИ CALLBACK КНОПОК ---

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = str(call.from_user.id)
    
    if call.data == "auto_text":
        card_url = user_cards.get(user_id)
        if not card_url:
            bot.answer_callback_query(call.id, "❌ Карточка не найдена. Отправьте фото заново.")
            return
        
        bot.answer_callback_query(call.id, "⏳ Генерирую описание...")
        wait_msg = bot.send_message(call.message.chat.id, "⏳ Генерирую AI-описание товара...")
        
        # Генерация описания
        description = generate_product_description(card_url)
        if not description:
            description = "Премиум товар"
        
        bot.edit_message_text(f"⏳ Описание готово!\n\n📝 <b>{description}</b>\n\nНакладываю текст на изображение...", 
                              call.message.chat.id, wait_msg.message_id, parse_mode="HTML")
        
        # Наложение текста
        final_image = add_premium_text_to_image(card_url, description)
        
        try:
            bot.delete_message(call.message.chat.id, wait_msg.message_id)
        except:
            pass
        
        if final_image:
            bot.send_photo(
                call.message.chat.id, 
                final_image, 
                caption=f"✅ <b>Готовая карточка с премиум-дизайном!</b>\n\n📝 {description}",
                parse_mode="HTML"
            )
        else:
            bot.send_message(call.message.chat.id, "❌ Не удалось наложить текст. Вот карточка без текста:")
            bot.send_photo(call.message.chat.id, card_url)
    
    elif call.data == "custom_text":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id, 
            "✍️ <b>Введите текст для карточки:</b>\n\n"
            "Можно использовать:\n"
            "• Название товара\n"
            "• Ключевые характеристики\n"
            "• Акцию или скидку\n\n"
            "Рекомендуется 2-5 слов для лучшего вида.",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, process_custom_text)
    
    elif call.data == "no_text":
        card_url = user_cards.get(user_id)
        if card_url:
            bot.answer_callback_query(call.id, "✅ Отправляю без текста")
            bot.send_photo(
                call.message.chat.id, 
                card_url, 
                caption="✅ <b>Карточка без текста</b>",
                parse_mode="HTML"
            )
        else:
            bot.answer_callback_query(call.id, "❌ Карточка не найдена")

def process_custom_text(message):
    """Обработка пользовательского текста"""
    user_id = str(message.from_user.id)
    card_url = user_cards.get(user_id)
    
    if not card_url:
        bot.send_message(message.chat.id, "❌ Карточка не найдена. Отправьте фото заново.")
        return
    
    custom_text = message.text.strip()
    
    if len(custom_text) > 100:
        bot.send_message(message.chat.id, "⚠️ Текст слишком длинный (максимум 100 символов). Попробуйте короче.")
        return
    
    wait_msg = bot.send_message(message.chat.id, f"⏳ Накладываю текст:\n\n<b>{custom_text}</b>", parse_mode="HTML")
    
    # Наложение текста
    final_image = add_premium_text_to_image(card_url, custom_text)
    
    try:
        bot.delete_message(message.chat.id, wait_msg.message_id)
    except:
        pass
    
    if final_image:
        bot.send_photo(
            message.chat.id, 
            final_image, 
            caption=f"✅ <b>Готовая карточка с премиум-дизайном!</b>\n\n📝 {custom_text}",
            parse_mode="HTML"
        )
    else:
        bot.send_message(message.chat.id, "❌ Не удалось наложить текст.")

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
    return "🤖 Бот работает! Версия 2.0 с премиум-дизайном карточек"

@app.route('/health')
def health():
    return {'status': 'ok', 'users': len(user_cards)}, 200

# --- ЗАПУСК ---

# --- ДИАГНОСТИКА ШРИФТОВ ПРИ СТАРТЕ ---
print("🔍 Проверка шрифтов...")
test_fonts = ['/app/Font bold.ttf', '/app/font_bold.ttf', 'Font bold.ttf', '/app/font.ttf', 'font.ttf']
for f in test_fonts:
    if os.path.exists(f):
        try:
            font = ImageFont.truetype(f, 40)
            bbox = font.getbbox("Тест русского текста")
            width = bbox[2] - bbox[0] if bbox else 0
            print(f"✅ {f}: ширина 'Тест' = {width}px")
        except Exception as e:
            print(f"❌ {f}: {e}")
    else:
        print(f"⚠️ {f}: файл не найден")

# Проверим, какие системные шрифты доступны
import glob
system_font_paths = glob.glob('/usr/share/fonts/truetype/**/*.ttf', recursive=True)
if system_font_paths:
    print(f"📁 Найдено системных шрифтов: {len(system_font_paths)}")
    for sf in system_font_paths[:5]:
        print(f"   → {sf}")
else:
    print("⚠️ Системные шрифты не найдены")
# --- КОНЕЦ ДИАГНОСТИКИ ---

if __name__ == '__main__':
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"✅ Webhook установлен: https://{railway_url}/webhook")
    
    print("🚀 Бот запущен с профессиональным премиум-дизайном!")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
