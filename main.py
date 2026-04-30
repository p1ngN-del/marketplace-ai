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
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    response = hf_client.chat.completions.create(
        model="Qwen/Qwen2.5-VL-72B-Instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"image/jpeg;base64,{base64_image}"}},
                    {"type": "text", "text": "Опиши товар кратко: Название, Цвет."}
                ]
            }
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def generate_background(description):
    prompt = f"Professional product photography background for {description}. Minimalist, clean, soft lighting, 8k."
    output = replicate.run(
        "black-forest-labs/flux-dev",
        input={"prompt": prompt, "aspect_ratio": "3:4", "output_quality": 90}
    )
    return output[0]

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото, я сделаю описание и фон.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Думаю... (это может занять до 1 мин)")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 1. Анализ
        desc = analyze_photo(downloaded_file)
        bot.send_message(message.chat.id, f"📝 Описание: {desc}")
        
        # 2. Фон
        bg_url = generate_background(desc)
        bot.send_photo(message.chat.id, bg_url, caption="🎨 Вот твой новый фон!")
        
        bot.delete_message(message.chat.id, wait_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Ошибка: {e}")
        print(e) # Чтобы увидеть ошибку в логах Railway

print("Бот запущен...")
bot.infinity_polling()
