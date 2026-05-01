import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import dashscope
from dashscope import MultiModalConversation
import os
import sys

# Если переменная BOT_ACTIVE не равна "true" — бот не запускается
if os.environ.get("BOT_ACTIVE", "true").lower() != "true":
    print("Бот отключен через переменную BOT_ACTIVE")
    sys.exit(0)
    
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
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}, {"type": "text", "text": "Опиши этот товар для маркетплейса. Верни ответ СТРОГО в формате JSON: {\"name\": \"название\", \"color\": \"основной цвет\", \"material\": \"материал\"}. Без лишнего текста."}]}]
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=messages,
            max_tokens=150
        )
        import json
        try:
            data = json.loads(response.choices[0].message.content)
            return data
        except:
            return {"name": "товар", "color": "белый", "material": "пластик"}
    except:
        return {"name": "товар", "color": "белый", "material": "пластик"}

def generate_description(product_data):
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
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return response.choices[0].message.content
    except:
        return "Стильный и надежный товар для вашего комфорта."

def parse_ai_response(ai_text):
    import re
    title_match = re.search(r"SEO-ЗАГОЛОВОК.*:(.*)", ai_text)
    title = title_match.group(1).strip() if title_match else "Лучший выбор"
    
    phrases_match = re.search(r"Инфографика.*:(.*)", ai_text)
    if phrases_match:
        phrases_text = phrases_match.group(1)
        phrases = [p.strip() for p in phrases_text.split('-') if len(p.strip()) > 3]
    else:
        phrases = ["Премиум качество", "Стильный дизайн", "Выгодная цена"]
    
    bg_match = re.search(r"Главное фото.*:(.*)", ai_text)
    bg_description = bg_match.group(1).strip() if bg_match else "минималистичный студийный фон"
    
    return {
        "title": title,
        "phrases": phrases,
        "bg_description": bg_description
    }

# --- РАБОТА С ИЗОБРАЖЕНИЕМ (ДВЕ МОДЕЛИ) ---
def retouch_photo(product_bytes):
    """Шаг 1: Ретушь и чистка фото с помощью Qwen-Image-Edit-Plus."""
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"

        prompt = "Удали лишние объекты с фотографии (руки, провода, блики от лампы). Оставь только сам товар. Помести его на нейтральный, чистый, студийный белый фон."

        messages = [{"role": "user", "content": [{"image": image_url}, {"text": prompt}]}]

        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-image-edit-plus",
            messages=messages,
            n=1, watermark=False, size="1024*1536"
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
        else:
            return None
    except:
        return None

def create_card(product_url, title, phrases):
    """Шаг 2: Создание стильной карточки с помощью Qwen-Image-2.0-Pro."""
    try:
        prompt = f"""Создай премиальную карточку товара для Wildberries.
        ПРАВИЛА ДИЗАЙНА:
        1. **Фон:** Чистый, студийный, с мягким градиентом. Товар занимает 60-70% пространства слева или по центру. Никакого лишнего реквизита.
        2. **Текст (строго на русском!):**
           - Заголовок (КРУПНО): "{title}".
           - Преимущества (в столбик справа): {', '.join(phrases)}.
        3. **Иерархия:**
           - Заголовок — самый крупный и жирный шрифт.
           - Преимущества — средний размер, каждая фраза с новой строки.
        4. **Расположение:** Справа от товара, на полупрозрачной плашке для контраста.
        5. **Стиль:** Современный, минималистичный, дорогой. Никаких кричащих цветов.
        Создай изображение.
        """

        messages = [{"role": "user", "content": [{"image": product_url}, {"text": prompt}]}]

        response = MultiModalConversation.call(
            api_key=DASHSCOPE_API_KEY,
            model="qwen-image-2.0-pro",
            messages=messages,
            n=1, watermark=False, size="1024*1536"
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]['image']
        else:
            return None
    except:
        return None

# --- TELEGRAM БОТ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото товара, и я сделаю из него карточку для маркетплейса с описанием.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Создаю карточку... Это займёт около 40-50 секунд.")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        product_data = analyze_photo(downloaded_file)
        ai_text = generate_description(product_data)
        content = parse_ai_response(ai_text)
        
        # 1. Ретушь фото
        retouched_url = retouch_photo(downloaded_file)
        if not retouched_url:
            bot.send_message(message.chat.id, "❌ Не удалось обработать фото.")
            return
        
        # 2. Создание дизайнерской карточки
        card_url = create_card(retouched_url, content['title'], content['phrases'])
        
        if card_url:
            bot.send_photo(message.chat.id, card_url, caption=f"✅ Готовая карточка!\n{' | '.join(content['phrases'])}")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось создать карточку.")
            
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
