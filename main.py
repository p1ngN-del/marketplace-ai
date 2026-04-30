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
    """Генерирует короткие УТП и преимущества на основе данных о товаре."""
    try:
        prompt = f"Ты — маркетолог. Придумай для товара '{product_data['name']}' (цвет: {product_data['color']}, материал: {product_data['material']}) 3 коротких продающих преимущества. Каждое преимущество — строго до 5 слов. Ответь на русском языке, каждое преимущество с новой строки."
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        # Разбиваем ответ на список преимуществ
        lines = response.choices[0].message.content.split('\n')
        return [line.strip() for line in lines if line.strip()][:3]
    except:
        return ["Премиум качество", "Стильный дизайн", "Выгодная цена"]

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

def add_text_overlay(image_url, features):
    """Накладывает стильный и ЧИТАЕМЫЙ текст с тенью."""
    try:
        # Загружаем изображение по URL
        with urllib.request.urlopen(image_url) as f:
            img = Image.open(io.BytesIO(f.read())).convert("RGBA")

        draw = ImageDraw.Draw(img)
        
        # Загружаем наш скачанный шрифт
        try:
            font_large = ImageFont.truetype("font.ttf", 60)
            font_small = ImageFont.truetype("font.ttf", 30)
        except:
            # Если шрифт не найден, используем стандартный (запасной вариант)
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # Функция для рисования текста с тенью (для читаемости)
        def draw_text_with_shadow(x, y, text, font, fill=(255, 255, 255)):
            # Рисуем тень (черная, со смещением)
            draw.text((x+2, y+2), text, font=font, fill=(0, 0, 0))
            # Рисуем основной текст поверх
            draw.text((x, y), text, font=font, fill=fill)

        # --- 1. Заголовок УТП (вверху слева) ---
        draw_text_with_shadow(30, 30, "ХИТ ПРОДАЖ", font_large, fill=(255, 80, 80)) # Красный цвет для заголовка

        # --- 2. Преимущества (справа по центру) ---
        x_pos = int(img.width * 0.55)
        y_pos = int(img.height * 0.4)
        for i, feature in enumerate(features):
            draw_text_with_shadow(x_pos, y_pos + i * 50, f"• {feature}", font_small)

        # Сохраняем результат в память
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
        
        # 2. Генерация продающих преимуществ
        features = generate_description(product_data)
        
        # 3. Создание картинки с динамическим фоном
        card_url = create_card(downloaded_file, product_data)
        
        if card_url:
            # 4. Стильное наложение текста
            final_card = add_text_overlay(card_url, features)
            if final_card:
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готовая карточка!\n{' | '.join(features)}")
            else:
                bot.send_photo(message.chat.id, card_url, caption=f"✅ Карточка создана, но текст наложить не удалось.\n{' | '.join(features)}")
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
