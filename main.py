import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import tempfile

# --- НАСТРОЙКИ ---
# Токены берем из переменных окружения Railway
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
RAILWAY_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN") # Railway сам создаст эту переменную

if not TG_TOKEN or not HF_TOKEN:
    raise Exception("Не хватает токенов TG_TOKEN или HF_TOKEN в переменных среды!")

# Инициализация бота и клиента HF
bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)

# --- ЛОГИКА АНАЛИЗА ---
def analyze_photo_from_bytes(photo_bytes):
    try:
        # Кодируем фото в base64
        base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Ты помощник селлера. Опиши товар для карточки маркетплейса. Верни: 1. Название. 2. Характеристики (цвет, материал). 3. Преимущества."}
                    ]
                }
            ],
            max_tokens=400
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Ошибка анализа: {str(e)}"

# --- ОБРАБОТЧИКИ TELEGRAM ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! 📸 Пришли мне фото товара, и я составлю для него описание.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    # Отправляем сообщение "Думаю...", чтобы пользователь не ждал в тишине
    wait_msg = bot.reply_to(message, "🔍 Анализирую фото... Подождите секунд 10-20...")
    
    try:
        # Получаем файл фото из Telegram
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Анализируем
        description = analyze_photo_from_bytes(downloaded_file)
        
        # Редактируем сообщение "Думаю..." на результат
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"✅ Готово!\n\n{description}")
        
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Произошла ошибка: {e}")

# --- ЗАПУСК WEBHOOK (для Railway) ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return '403'

@app.route('/')
def index():
    return "Bot is running on Railway!"

if __name__ == '__main__':
    # Настраиваем Webhook при запуске
    # Railway дает публичный домен в переменной RAILWAY_PUBLIC_DOMAIN или мы можем взять его из headers
    # Но проще всего использовать стандартный порт и пусть Railway проксирует
    
    # Для локального теста можно использовать bot.polling(), но для Railway нужен webhook
    # Мы используем простой трюк: запускаем Flask, а бота подключаем через webhook вручную один раз
    # Или используем polling, если Railway позволяет долгоживущие процессы (он позволяет)
    
    # ВАРИАНТ ДЛЯ RAILWAY (Polling проще для старта, но Webhook надежнее)
    # Давайте используем Polling, так как это проще для новичка. 
    # Railway не убивает процесс, если он пишет в лог.
    
    print("Запуск бота в режиме Polling...")
    bot.infinity_polling()
