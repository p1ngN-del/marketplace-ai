import os
import telebot
from huggingface_hub import InferenceClient
import base64
import replicate
from rembg import remove
from PIL import Image
import io
import urllib.request

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

def create_card(product_bytes, bg_url):
    try:
        # 1. Вырезаем товар
        input_img = Image.open(io.BytesIO(product_bytes))
        no_bg = remove(input_img) # Это может занять 10-15 сек на CPU
        
        # 2. Качаем фон
        with urllib.request.urlopen(bg_url) as resp:
            bg_img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            
        # 3. Масштабируем товар (70% от высоты фона)
        ratio = 0.7
        h = int(bg_img.height * ratio)
        w = int(no_bg.width * (h / float(no_bg.height)))
        no_bg = no_bg.resize((w, h), Image.LANCZOS)
        
        # 4. Вставляем по центру
        pos = ((bg_img.width - w) // 2, (bg_img.height - h) // 2)
        bg_img.paste(no_bg, pos, no_bg)
        
        # 5. Сохраняем
        out = io.BytesIO()
        bg_img.save(out, format='PNG')
        out.seek(0)
        return out
    except Exception as e:
        print(f"Ошибка склейки: {e}")
        return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Пришли фото, я сделаю карточку.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Работаю: вырезаю товар, рисую фон... (ждите ~40 сек)")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        desc = analyze_photo(downloaded_file)
        bg_url = generate_background(desc)
        
        if bg_url:
            final_card = create_card(downloaded_file, bg_url)
            if final_card:
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готово!\n{desc}")
            else:
                bot.send_photo(message.chat.id, bg_url, caption=f"❌ Не удалось склеить, но вот фон.\n{desc}")
        else:
            bot.send_message(message.chat.id, "Ошибка генерации фона.")
            
        bot.delete_message(message.chat.id, wait_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Ошибка: {e}")

print("Бот запущен...")
bot.infinity_polling()
