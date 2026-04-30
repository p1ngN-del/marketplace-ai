import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import replicate
from PIL import Image
import io
import urllib.request
import requests

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
REPLICATE_TOKEN = os.environ.get("REPLICATE_TOKEN")
REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY")

if not all([TG_TOKEN, HF_TOKEN, REPLICATE_TOKEN, REMOVEBG_API_KEY]):
    raise Exception("Не хватает токенов! Проверьте переменные на Railway.")

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN

bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)
app = Flask(__name__)

# --- ФУНКЦИИ ОБРАБОТКИ ---
def analyze_photo(image_bytes):
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}, {"type": "text", "text": "Опиши товар кратко: Название и цвет."}]}],
            max_tokens=50
        )
        return response.choices[0].message.content
    except:
        return "Product"

def generate_background(desc):
    try:
        prompt = f"Minimalist professional background for {desc}. Soft lighting, clean, 8k, empty space in center."
        output = replicate.run("black-forest-labs/flux-dev", input={"prompt": prompt, "aspect_ratio": "3:4", "output_quality": 90})
        return output[0]
    except:
        return None

def remove_bg(image_bytes):
    """Удаляет фон через API remove.bg"""
    try:
        response = requests.post(
            'https://api.remove.bg/v1.0/removebg',
            files={'image_file': ('image.jpg', image_bytes)},
            data={'size': 'auto'},
            headers={'X-Api-Key': REMOVEBG_API_KEY},
        )
        if response.status_code == 200:
            return response.content
        else:
            print(f"Ошибка remove.bg: {response.status_code}")
            return None
    except Exception as e:
        print(f"Ошибка remove.bg: {e}")
        return None

def create_card(product_bytes, bg_url):
    """Склеивает товар без фона с новым фоном"""
    try:
        # 1. Удаляем фон
        no_bg_bytes = remove_bg(product_bytes)
        if not no_bg_bytes:
            return None
            
        no_bg_img = Image.open(io.BytesIO(no_bg_bytes))
        
        # 2. Качаем фон
        with urllib.request.urlopen(bg_url) as resp:
            bg_img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            
        # 3. Подгоняем размер
        ratio = 0.7
        h = int(bg_img.height * ratio)
        w = int(no_bg_img.width * (h / float(no_bg_img.height)))
        no_bg_img = no_bg_img.resize((w, h), Image.LANCZOS)
        
        # 4. Вставляем в центр
        pos = ((bg_img.width - w) // 2, (bg_img.height - h) // 2)
        bg_img.paste(no_bg_img, pos, no_bg_img)
        
        # 5. Сохраняем
        out = io.BytesIO()
        bg_img.save(out, format='PNG')
        out.seek(0)
        return out
    except Exception as e:
        print(f"Ошибка склейки: {e}")
        return None

# --- TELEGRAM БОТ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото, я сделаю карточку.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Создаю карточку... (до 30 секунд)")
    
    try:
        # Скачиваем фото
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Анализируем
        desc = analyze_photo(downloaded_file)
        
        # Генерируем фон
        bg_url = generate_background(desc)
        
        if bg_url:
            # Склеиваем
            final_card = create_card(downloaded_file, bg_url)
            
            if final_card:
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готовая карточка!\n{desc}")
            else:
                bot.send_photo(message.chat.id, bg_url, caption=f"⚠️ Не удалось вырезать товар (лимит remove.bg?). Вот фон к вашему товару.")
        else:
            bot.send_message(message.chat.id, "❌ Ошибка генерации фона.")
            
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
