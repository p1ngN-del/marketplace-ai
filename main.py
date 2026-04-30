import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import dashscope
from dashscope import MultiModalConversation
from PIL import Image, ImageDraw, ImageFont
import io
import urllib.request
import random

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")

if not all([TG_TOKEN, HF_TOKEN, DASHSCOPE_API_KEY]):
    raise Exception("Не хватает токенов! Проверьте переменные на Railway.")

dashscope.base_http_api_url = 'https://dashscope-intl.aliyuncs.com/api/v1'

bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)
app = Flask(__name__)

# --- ГЕНЕРАЦИЯ МАРКЕТИНГОВЫХ ДАННЫХ ---
def analyze_photo(image_bytes):
    """Анализирует фото и возвращает словарь с информацией о товаре."""
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}, {"type": "text", "text": "Опиши этот товар для маркетплейса. Верни ответ СТРОГО в формате JSON: {\"name\": \"название\", \"color\": \"основной цвет\", \"material\": \"материал\"}. Без лишнего текста."}]}]
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=messages,
            max_tokens=150
        )
        # Пытаемся распарсить JSON, если не выходит — возвращаем заглушку
        import json
        try:
            data = json.loads(response.choices[0].message.content)
            return data
        except:
            return {"name": "товар", "color": "белый", "material": "пластик"}
    except:
        return {"name": "товар", "color": "белый", "material": "пластик"}

def generate_description(product_data):
    """Генерирует полное маркетинговое ТЗ на основе вашего промпта."""
    try:
        prompt = f"""Ты — ведущий e-commerce-стратег.
        Товар: {product_data['name']}, цвет: {product_data['color']}, материал: {product_data['material']}.
        СГЕНЕРИРУЙ КОНТЕНТ ЧЕТКО ПО СТРУКТУРЕ:
        1. SEO-ЗАГОЛОВОК (до 120 символов): Сгенерируй 1 вариант.
        2. ПРОДАЮЩЕЕ ОПИСАНИЕ (200-300 символов, без воды):
           - Крючок (1 предл.)
           - Ключевые фишки (буллиты, 3-4 шт.)
        3. ТЕХНИЧЕСКОЕ ЗАДАНИЕ (ТЗ) ДЛЯ ГЕНЕРАЦИИ ФОНА:
           - Главное фото: Опиши идеальный фон для этого товара.
           - Инфографика: 3-4 коротких фразы (до 5 слов) для плашек на карточке.
        Ответь СТРОГО на русском языке."""
        
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct", # Используем текстовую модель для генерации текста
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return response.choices[0].message.content
    except:
        return "Стильный и надежный товар для вашего комфорта."

def parse_ai_response(ai_text):
    """Парсит ответ ИИ и извлекает заголовок, преимущества и фон."""
    import re
    # Извлекаем заголовок
    title_match = re.search(r"SEO-ЗАГОЛОВОК.*:(.*)", ai_text)
    title = title_match.group(1).strip() if title_match else "Лучший выбор"
    
    # Извлекаем фразы для инфографики
    phrases_match = re.search(r"Инфографика.*:(.*)", ai_text)
    if phrases_match:
        phrases_text = phrases_match.group(1)
        phrases = [p.strip() for p in phrases_text.split('-') if len(p.strip()) > 3]
    else:
        phrases = ["Премиум качество", "Стильный дизайн", "Выгодная цена"]
    
    # Извлекаем фон для ТЗ
    bg_match = re.search(r"Главное фото.*:(.*)", ai_text)
    bg_description = bg_match.group(1).strip() if bg_match else "минималистичный студийный фон"
    
    return {
        "title": title,
        "phrases": phrases,
        "bg_description": bg_description
    }

# --- РАБОТА С ИЗОБРАЖЕНИЕМ ---
def create_card(product_bytes, product_data):
    """Генерирует картинку с динамическим фоном."""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"

        # Динамический промпт для фона!
        prompt = f"""Создай профессиональную карточку товара для Wildberries.
        Товар: {product_data['name']}, цвет: {product_data['color']}, материал: {product_data['material']}.
        Инструкция:
        1. Помести товар на фон, который ИДЕАЛЬНО подходит по стилю и цвету к этому товару.
        2. Оставь достаточно свободного пространства для текста.
        3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО рисовать любой текст или буквы на изображении.
        4. Только фон и товар. Никаких лишних предметов.
        """

        messages = [{
            "role": "user",
            "content": [
                {"image": image_url},
                {"text": prompt}
            ]
        }]

        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-image-edit-plus",
            messages=messages,
            n=1,
            watermark=False,
            size="1024*1536"
        )

        if response.status_code == 200:
            result_url = response.output.choices[0].message.content[0]['image']
            return result_url
        else:
            return None
    except:
        return None

def add_text_overlay(image_url, title, phrases):
    """Накладывает стильный текст шрифтом Montserrat."""
    try:
        with urllib.request.urlopen(image_url) as f:
            img = Image.open(io.BytesIO(f.read())).convert("RGBA")
        draw = ImageDraw.Draw(img)

        # Загружаем НАШИ шрифты Montserrat
        try:
            # Явно указываем файлы, которые загрузили на GitHub
            font_title = ImageFont.truetype("font_bold.ttf", 75)  # Жирный для заголовка H1
            font_text = ImageFont.truetype("font_regular.ttf", 35) # Обычный для преимуществ H2
        except Exception as font_error:
            print(f"Ошибка загрузки шрифта Montserrat: {font_error}. Использую стандартный.")
            font_title = ImageFont.load_default()
            font_text = ImageFont.load_default()

        # --- Заголовок (H1) ---
        title_x = int(img.width / 2)
        title_y = 40
        # Тень для читаемости по правилам контраста
        draw.text((title_x+3, title_y+3), title, font=font_title, fill=(0, 0, 0, 180), anchor="mt")
        draw.text((title_x, title_y), title, font=font_title, fill=(255, 255, 255), anchor="mt")

        # --- Офферы (H2) ---
        y_start = int(img.height * 0.25)
        for i, phrase in enumerate(phrases):
            y_pos = y_start + i * 65 # Увеличили отступ, чтобы текст "дышал" (правило "воздуха")
            # Плашка для контраста
            draw.rounded_rectangle([20, y_pos, 400, y_pos + 50], radius=5, fill=(0, 0, 0, 150))
            # Белый текст на темной плашке - идеальный контраст по правилам статьи
            draw.text((35, y_pos + 5), phrase, font=font_text, fill=(255, 255, 255))

        output = io.BytesIO()
        img.save(output, format='PNG')
        output.seek(0)
        return output
    except Exception as e:
        print(f"Ошибка наложения текста: {e}")
        return None

# --- TELEGRAM БОТ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото товара, и я сделаю из него карточку для маркетплейса с описанием.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Создаю карточку... Это займёт около 30-40 секунд.")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 1. Анализ товара (получаем данные)
        product_data = analyze_photo(downloaded_file)
        
        # 2. Генерация ТЗ по вашему промпту
        ai_text = generate_description(product_data)
        content = parse_ai_response(ai_text)
        
        # 3. Создание картинки с фоном от ИИ
        product_data['bg_description'] = content['bg_description'] # Передаем фон
        card_url = create_card(downloaded_file, product_data)
        
        if card_url:
            # 4. Стильное наложение текста (исправленная строка)
            final_card = add_text_overlay(card_url, content['title'], content['phrases'])
            if final_card:
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готовая карточка!\n{' | '.join(content['phrases'])}")
            else:
                bot.send_photo(message.chat.id, card_url, caption=f"✅ Карточка создана, но текст наложить не удалось.\n{' | '.join(content['phrases'])}")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось создать карточку. Попробуйте другое фото.")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    finally:
        bot.delete_message(message.chat.id, wait_msg.message_id)

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
