import os
import telebot
from huggingface_hub import InferenceClient
import base64
import replicate
import requests # Добавили для проверки

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
REPLICATE_TOKEN = os.environ.get("REPLICATE_TOKEN")

if not all([TG_TOKEN, HF_TOKEN, REPLICATE_TOKEN]):
    raise Exception("Не хватает токенов!")

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN

bot = telebot.TeleBot(TG_TOKEN)
# Инициализируем клиент HF
hf_client = InferenceClient(token=HF_TOKEN)

def analyze_photo(image_bytes):
    try:
        # Кодируем в base64
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        # Пробуем отправить запрос
        response = hf_client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Опиши этот товар одним предложением. Только суть (например: 'Черные кожаные кроссовки')."}
                    ]
                }
            ],
            max_tokens=50
        )
        text = response.choices[0].message.content
        return text if text else "Товар на фото"
        
    except Exception as e:
        print(f"Ошибка HF: {e}")
        return "Стильный товар" # Заглушка, если HF упал

def generate_background(description):
    try:
        # Если описание слишком длинное или странное, обрезаем
        safe_desc = description[:50] 
        
        prompt = f"Professional studio background for product photography. Style: minimalist, clean, soft lighting, high quality, 8k. Product context: {safe_desc}. Empty space in center."
        
        output = replicate.run(
            "black-forest-labs/flux-dev",
            input={
                "prompt": prompt, 
                "aspect_ratio": "3:4", 
                "output_quality": 90,
                "num_inference_steps": 28 # Чуть быстрее
            }
        )
        return output[0]
    except Exception as e:
        print(f"Ошибка Replicate: {e}")
        return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото товара. Я сделаю описание и сгенерирую фон.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Думаю... (секунд 15-30)")
    
    try:
        # 1. Скачиваем фото
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 2. Анализируем (получаем описание)
        desc = analyze_photo(downloaded_file)
        
        # 3. Генерируем фон
        bg_url = generate_background(desc)
        
        if bg_url:
            # Присылаем фон
            bot.send_photo(message.chat.id, bg_url, caption=f"📝 Описание: {desc}\n\n🎨 Вот твой новый фон!")
        else:
            bot.send_message(message.chat.id, f"❌ Не удалось создать фон. Но вот описание:\n{desc}")
            
        # Удаляем сообщение "Ждите"
        bot.delete_message(message.chat.id, wait_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Общая ошибка: {e}")
        print(e)

print("Бот запущен...")
bot.infinity_polling()
