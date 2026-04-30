import os
import telebot
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

def remove_via_api(image_bytes):
    """Быстрое удаление фона через remove.bg"""
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
            print(f"Ошибка remove.bg: {response.status_code}, {response.text}")
            return None
    except Exception as e:
        print(f"Ошибка связи с remove.bg: {e}")
        return None

def create_card(product_bytes, bg_url):
    try:
        # 1. Удаляем фон через быстрый API
        print("Удаляем фон...")
        no_bg_bytes = remove_via_api(product_bytes)
        if not no_bg_bytes:
            return None # Возвращаем None, если не удалось
            
        no_bg_img = Image.open(io.BytesIO(no_bg_bytes))
        
        # 2. Качаем сгенерированный фон
        print("Качаем фон...")
        with urllib.request.urlopen(bg_url) as resp:
            bg_img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            
        # 3. Подгоняем размер товара (70% от высоты фона)
        print("Склеиваем...")
        ratio = 0.7
        h = int(bg_img.height * ratio)
        w = int(no_bg_img.width * (h / float(no_bg_img.height)))
        no_bg_img = no_bg_img.resize((w, h), Image.LANCZOS)
        
        # 4. Вставляем в центр
        pos = ((bg_img.width - w) // 2, (bg_img.height - h) // 2)
        bg_img.paste(no_bg_img, pos, no_bg_img)
        
        # 5. Сохраняем в память
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
    wait_msg = bot.reply_to(message, "⏳ Начинаю обработку... (займет до 30 секунд)")
    
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
                bot.send_photo(message.chat.id, final_card, caption=f"✅ Готово!\n{desc}")
            else:
                # Если склейка не удалась (например, кончился лимит remove.bg), шлем просто фон
                bot.send_photo(message.chat.id, bg_url, caption=f"✅ Описание: {desc}\n\n⚠️ Не удалось склеить (лимит remove.bg исчерпан?), но вот фон.")
        else:
            bot.send_message(message.chat.id, "❌ Ошибка генерации фона.")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Общая ошибка: {e}")
    finally:
        bot.delete_message(message.chat.id, wait_msg.message_id)

print("Бот запущен...")
bot.infinity_polling()
