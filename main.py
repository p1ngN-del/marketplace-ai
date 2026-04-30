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
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}, {"type": "text", "text": "Опиши этот товар для карточки маркетплейса. Напиши строго на русском языке: что это и какого он цвета."}]}],
            max_tokens=50
        )
        return response.choices[0].message.content
    except:
        return "товар"

def create_card(product_bytes, description):
    try:
        base64_image = base64.b64encode(product_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{base64_image}"

        prompt = f"""Создай профессиональную карточку товара для Wildberries на основе этого изображения.
        Товар: {description}.
        Требования к фону:
        - Помести товар на минималистичный, светлый, студийный фон.
        - Оставь свободное пространство слева или справа от товара для будущего текста.
        - Никакого текста на изображении! Только фон и товар.
        - Стиль: чистый, коммерческий, высокое качество.
        - Освещение: мягкое, студийное.
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
            print(f"Ошибка Qwen-Image-Edit: {response.message}")
            return None
    except Exception as e:
        print(f"Ошибка создания карточки: {e}")
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

        # Настройки текста
        # ВАЖНО: Загрузите любой .ttf шрифт в папку с main.py и пропишите путь к нему.
        # font_large = ImageFont.truetype("путь_к_вашему_шрифту.ttf", 60)
        # font_small = ImageFont.truetype("путь_к_вашему_шрифту.ttf", 30)
        # Пока используем стандартный шрифт
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
        
        # Координаты для текста (справа, занимает 40% ширины)
        x_pos = int(img.width * 0.55)
        y_pos_top = 50

        # Рисуем полупрозрачную плашку под текст
        panel_width = int(img.width * 0.4)
        panel_height = img.height - 100
        draw.rectangle([x_pos - 10, y_pos_top - 10, x_pos + panel_width, y_pos_top + panel_height], fill=(255, 255, 255, 150))

        # Заголовок УТП
        draw.text((x_pos, y_pos_top + 20), "🔥 ХИТ ПРОДАЖ", fill=(0, 0, 0), font=font_large)
        
        # Характеристики
        draw.text((x_pos, y_pos_top + 120), f"{description}", fill=(50, 50, 50), font=font_small)
        draw.text((x_pos, y_pos_top + 180), "✔ Премиум качество", fill=(50, 50, 50), font=font_small)
        draw.text((x_pos, y_pos_top + 230), "✔ Быстрая доставка", fill=(50, 50, 50), font=font_small)
        draw.text((x_pos, y_pos_top + 280), "✔ Выгодная цена", fill=(50, 50, 50), font=font_small)

        # Накладываем слой на изображение
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
    bot.reply_to(message, "Привет! Пришли фото, и я превращу его в карточку для маркетплейса.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Создаю карточку... Это займёт около 20-30 секунд.")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        desc = analyze_photo(downloaded_file)
        card_url = create_card(downloaded_file, desc)
        
        if card_url:
            # Добавляем текст
            final_card = add_text_overlay(card_url, desc)
            if final_card:
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готовая карточка!\n{desc}")
            else:
                bot.send_photo(message.chat.id, card_url, caption=f"✅ Карточка создана, но текст наложить не удалось.\n{desc}")
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
