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

# --- ФУНКЦИИ ОБРАБОТКИ ---
def analyze_photo(image_bytes):
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}, {"type": "text", "text": "Опиши этот товар кратко, одним предложением. Строго на русском языке."}]}]
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=messages,
            max_tokens=100
        )
        return response.choices[0].message.content
    except:
        return "современный гаджет"

def generate_description(product_info):
    try:
        prompt = f"Придумай короткое (до 300 символов) продающее описание для этого товара на Wildberries: '{product_info}'. Напиши 2-3 преимущества. Ответь строго на русском языке."
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200
        )
        return response.choices[0].message.content
    except:
        return product_info

def create_card(product_bytes, description):
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"

        # Промпт, который ЗАПРЕЩАЕТ нейросети писать текст
        prompt = f"""На основе этого изображения создай карточку товара для Wildberries.
        Товар: {description}.
        Инструкция:
        1. Помести товар на минималистичный, светлый, студийный фон.
        2. Оставь много свободного пространства справа.
        3. НЕ ДОБАВЛЯЙ НИКАКОГО ТЕКСТА НА ИЗОБРАЖЕНИЕ. Абсолютно никакого!
        4. Только фон и товар.
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

def add_text_overlay(image_url, description):
    """Накладывает текст УТП на готовую картинку, используя PIL."""
    try:
        # Загружаем изображение по URL
        with urllib.request.urlopen(image_url) as f:
            img = Image.open(io.BytesIO(f.read())).convert("RGBA")

        # Создаем слой для рисования
        overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)

        # Используем стандартный шрифт
        font = ImageFont.load_default()
        
        # Координаты для текста
        x_pos = int(img.width * 0.55)
        y_pos_top = 80

        # Рисуем полупрозрачную плашку под текст
        panel_width = int(img.width * 0.4)
        panel_height = img.height - 120
        draw.rectangle([x_pos - 20, y_pos_top - 20, x_pos + panel_width + 20, y_pos_top + panel_height + 20], fill=(255, 255, 255, 180))

        # Разбиваем длинный текст на строки
        lines = []
        line = ""
        for word in description.split():
            # Примерная ширина строки для стандартного шрифта
            if len(line + word) < 40: 
                line += word + " "
            else:
                lines.append(line.strip())
                line = word + " "
        lines.append(line.strip())
        
        # Выводим текст по строкам
        y_offset = 0
        for line in lines:
            draw.text((x_pos, y_pos_top + y_offset), line, fill=(0, 0, 0), font=font)
            y_offset += 40

        # Накладываем слой с текстом на изображение
        img = Image.alpha_composite(img, overlay)
        
        # Сохраняем в поток
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
        
        # 1. Анализ товара
        product_info = analyze_photo(downloaded_file)
        
        # 2. Генерация продающего текста
        marketing_text = generate_description(product_info)
        
        # 3. Создание картинки с фоном
        card_url = create_card(downloaded_file, product_info)
        
        if card_url:
            # 4. Наложение продающего текста
            final_card = add_text_overlay(card_url, marketing_text)
            if final_card:
                # Отправляем готовую карточку
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готовая карточка!\n{marketing_text}")
            else:
                # Если наложить текст не вышло, отправляем хотя бы картинку
                bot.send_photo(message.chat.id, card_url, caption=f"✅ Карточка создана, но текст наложить не удалось.\n{marketing_text}")
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
