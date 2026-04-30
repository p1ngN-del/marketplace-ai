import os
import telebot
from flask import Flask, request
from huggingface_hub import InferenceClient
import base64
import replicate
from rembg import remove
from PIL import Image
import io

# --- НАСТРОЙКИ ---
TG_TOKEN = os.environ.get("TG_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
REPLICATE_TOKEN = os.environ.get("REPLICATE_TOKEN")

if not all([TG_TOKEN, HF_TOKEN, REPLICATE_TOKEN]):
    raise Exception("Не хватает токенов! Проверьте переменные среды в Railway.")

os.environ["REPLICATE_API_TOKEN"] = REPLICATE_TOKEN

bot = telebot.TeleBot(TG_TOKEN)
hf_client = InferenceClient(token=HF_TOKEN)

# --- ФУНКЦИИ ---

def analyze_photo(image_bytes):
    """Анализирует фото и возвращает описание"""
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    response = hf_client.chat.completions.create(
        model="Qwen/Qwen2.5-VL-72B-Instruct",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    {"type": "text", "text": "Опиши товар кратко: Название, Цвет, Материал."}
                ]
            }
        ],
        max_tokens=150
    )
    return response.choices[0].message.content

def generate_background(description):
    """Генерирует фон по описанию"""
    prompt = f"Professional product photography background for {description}. Minimalist, clean, soft lighting, 8k, empty space in center."
    output = replicate.run(
        "black-forest-labs/flux-dev",
        input={"prompt": prompt, "aspect_ratio": "3:4", "output_quality": 90}
    )
    return output[0] # Возвращает URL картинки

def create_card(original_photo_bytes, bg_url):
    """Вырезает товар и вставляет на новый фон"""
    try:
        # 1. Удаляем фон у исходного фото
        input_image = Image.open(io.BytesIO(original_photo_bytes))
        no_bg_image = remove(input_image)
        
        # 2. Скачиваем сгенерированный фон
        import urllib.request
        with urllib.request.urlopen(bg_url) as response:
            bg_image = Image.open(io.BytesIO(response.read())).convert("RGBA")
            
        # 3. Подгоняем размеры
        # Растягиваем товар так, чтобы он занимал 70% высоты фона
        ratio = 0.7
        new_height = int(bg_image.height * ratio)
        w_percent = (new_height / float(no_bg_image.size[1]))
        new_width = int((float(no_bg_image.size[0]) * float(w_percent)))
        no_bg_image = no_bg_image.resize((new_width, new_height), Image.LANCZOS)
        
        # 4. Вставляем товар в центр фона
        position = ((bg_image.width - new_width) // 2, (bg_image.height - new_height) // 2)
        bg_image.paste(no_bg_image, position, no_bg_image)
        
        # 5. Сохраняем результат в память
        img_byte_arr = io.BytesIO()
        bg_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        return img_byte_arr
        
    except Exception as e:
        print(f"Ошибка сборки карточки: {e}")
        return None

# --- БОТ ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! 📸 Пришли фото товара, я сделаю карточку для маркетплейса.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    wait_msg = bot.reply_to(message, "⏳ Работаю: анализирую, рисую фон, собираю карточку... (ждите ~40 сек)")
    
    try:
        # 1. Получаем фото
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 2. Анализ
        desc = analyze_photo(downloaded_file)
        
        # 3. Генерация фона
        bg_url = generate_background(desc)
        
        # 4. Сборка карточки
        final_card = create_card(downloaded_file, bg_url)
        
        if final_card:
            bot.send_photo(message.chat.id, final_card, caption=f"✅ Готово!\n\n{desc}")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось собрать карточку, но вот описание:\n" + desc)
            
        bot.delete_message(message.chat.id, wait_msg.message_id)
        
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id, text=f"❌ Ошибка: {e}")

# --- ЗАПУСК ---
print("Бот запущен...")
bot.infinity_polling()
