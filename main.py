import os
import telebot
from huggingface_hub import InferenceClient
import base64
import replicate

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
REPLICATE_TOKEN = os.environ.get("REPLICATE_TOKEN")

if not all([TG_TOKEN, HF_TOKEN, REPLICATE_TOKEN]):
    raise Exception("Не хватает токенов!")

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN

bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)

def analyze_photo(image_bytes):
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Опиши товар для маркетплейса: Название, Цвет, Материал. Кратко."}
                    ]
                }
            ],
            max_tokens=150
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Ошибка анализа: {e}"

def generate_background(description):
    try:
        # Упрощенный промпт для скорости
        prompt = f"Professional product background for {description}. Minimalist, studio lighting, 8k, empty space."
        output = replicate.run(
            "black-forest-labs/flux-dev",
            input={"prompt": prompt, "aspect_ratio": "3:4", "output_quality": 90}
        )
        return output[0]
    except Exception as e:
        return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото товара. Я сделаю описание и предложу фон.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    # Сразу отвечаем, чтобы Telegram не рвал соединение
    wait_msg = bot.reply_to(message, "⏳ Анализирую товар...")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 1. Анализ (обычно быстро, 5-10 сек)
        desc = analyze_photo(downloaded_file)
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"📝 Описание:\n{desc}\n\n🎨 Сейчас сгенерирую фон...")
        
        # 2. Генерация фона (может занять 15-30 сек)
        bg_url = generate_background(desc)
        
        if bg_url:
            bot.send_photo(message.chat.id, bg_url, caption="Вот вариант профессионального фона для этого товара!")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось сгенерировать фон (проверьте баланс Replicate).")
            
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Произошла ошибка: {e}")
        print(e)

print("Бот запущен...")
bot.infinity_polling()
