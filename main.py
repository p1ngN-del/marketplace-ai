import os
import telebot

TG_TOKEN = os.environ.get("TG_TOKEN")
bot = telebot.TeleBot(TG_TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Бот работает! Жду фото.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    # Просто отвечаем текстом, без сложной обработки
    bot.reply_to(message, "✅ Я получил твое фото! Сейчас бы его обработать...")

print("Бот запущен...")
bot.infinity_polling()
