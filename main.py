import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import replicate

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
REPLICATE_TOKEN = os.environ.get("REPLICATE_TOKEN")

if not all([TG_TOKEN, HF_TOKEN, REPLICATE_TOKEN]):
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
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Опиши товар кратко: Название и цвет."}
                    ]
                }
            ],
            max_tokens=50
        )
        return response.choices[0].message.content
    except:
        return "Product"

def generate_background(desc):
    try:
        prompt = f"Minimalist professional background for {desc}. Soft lighting, clean, 8k, empty space in center."
        output = replicate.run(
            "black-forest-labs/flux-dev",
            input={"prompt": prompt, "aspect_ratio": "3:4", "output_quality": 90}
        )
        return output[0]
    except:
        return None

# --- TELEGRAM БОТ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото, я сделаю карточку.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Обрабатываю... (секунд 15-20)")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        bot.send_photo(message.chat.id, downloaded_file, caption="📷 Ваше исходное фото")
        
        desc = analyze_photo(downloaded_file)
        bg_url = generate_background(desc)
        
        if bg_url:
            bot.send_photo(message.chat.id, bg_url, caption=f"🎨 Фон: {desc}\n\nНаложите товар на этот фон в любом фоторедакторе.")
        else:
            bot.send_message(message.chat.id, "❌ Ошибка генерации фона. Возможно, закончился баланс Replicate.")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Общая ошибка: {e}")
    finally:
        bot.delete_message(message.chat.id, wait_msg.message_id)

# --- WEBHOOK (Flask) ---
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
@app.route('/set_webhook')
def set_webhook():
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        return f"Webhook установлен: https://{railway_url}/webhook"
    else:
        # Если переменной нет, попробуем стандартный домен Railway
        return "Ошибка: не найдена переменная RAILWAY_PUBLIC_DOMAIN"    

if __name__ == '__main__':
    # Устанавливаем Webhook
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_url:
        bot.remove_webhook()
        bot.set_webhook(url=f"https://{railway_url}/webhook")
        print(f"Webhook установлен: https://{railway_url}/webhook")
    
    print("Бот запущен...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
