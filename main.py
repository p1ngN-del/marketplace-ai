# --- ОБНОВЛЁННАЯ finish_ai_mode ---
def finish_ai_mode(chat_id, user_id):
    # 1. СПРАШИВАЕМ ЗАГОЛОВОК
    msg = bot.send_message(chat_id, "✏️ Введите заголовок карточки (название товара):")
    bot.register_next_step_handler(msg, process_title_step, chat_id, user_id)

def process_title_step(message, chat_id, user_id):
    title = message.text.strip().upper()[:40] if message.text.strip() else "ТОВАР"
    bot.send_message(chat_id, "🧠 Генерирую текст через GPT-2...")
    generate_gpt2_texts(chat_id, user_id, title)

def generate_gpt2_texts(chat_id, user_id, title):
    analysis = user_analysis.get(user_id, {})
    answers = analysis.get('answers', [])
    questions = analysis.get('questions', [])
    
    # --- ФИЛЬТРУЕМ ОТВЕТЫ ---
    clean_pairs = []
    for i, ans in enumerate(answers):
        if not ans: 
            continue
        
        # Пропускаем отрицательные ответы
        if ans.strip().lower() in ["нет", "no", "нету", "отсутствует", "бонусов нет"]:
            continue
        if 'нет' in ans.lower() and len(ans) < 10:
            continue
            
        # Очищаем ответ
        question = questions[i] if i < len(questions) else ""
        clean_ans = clean_answer(ans, question)
        if not clean_ans or len(clean_ans) <= 1:
            continue
            
        clean_pairs.append((question, clean_ans))
    
    if not clean_pairs:
        bot.send_message(chat_id, "❌ Недостаточно данных для карточек")
        return
    
    # --- ГЕНЕРИРУЕМ ЧЕРЕЗ GPT-2 И ПОКАЗЫВАЕМ ПОЛЬЗОВАТЕЛЮ ---
    gpt2_results = []
    for question, answer in clean_pairs:
        generated = generate_description_gpt2(analysis, question, answer)
        gpt2_results.append({
            "question": question,
            "answer": answer,
            "generated": generated
        })
    
    # Сохраняем результаты для редактирования
    user_analysis[user_id]['gpt2_results'] = gpt2_results
    user_analysis[user_id]['title'] = title
    user_analysis[user_id]['edit_index'] = 0
    
    # Показываем первый результат
    show_gpt2_result(chat_id, user_id, 0)

def show_gpt2_result(chat_id, user_id, index):
    gpt2_results = user_analysis[user_id].get('gpt2_results', [])
    
    if index >= len(gpt2_results):
        # Все проверены — генерируем карточки
        generate_final_cards(chat_id, user_id)
        return
    
    item = gpt2_results[index]
    
    text = f"📝 <b>Вопрос:</b> {item['question']}\n💬 <b>Ваш ответ:</b> {item['answer']}\n✨ <b>GPT-2 предлагает:</b> {item['generated']}\n\nОставить или изменить?"
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Оставить", callback_data=f"gpt2_accept_{index}"),
        telebot.types.InlineKeyboardButton("✏️ Изменить", callback_data=f"gpt2_edit_{index}")
    )
    
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("gpt2_"))
def handle_gpt2_callback(call):
    user_id = str(call.from_user.id)
    data = call.data
    
    if data.startswith("gpt2_accept_"):
        index = int(data.replace("gpt2_accept_", ""))
        bot.answer_callback_query(call.id, "✅ Принято")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        # Переходим к следующему
        user_analysis[user_id]['edit_index'] = index + 1
        show_gpt2_result(call.message.chat.id, user_id, index + 1)
    
    elif data.startswith("gpt2_edit_"):
        index = int(data.replace("gpt2_edit_", ""))
        bot.answer_callback_query(call.id, "✏️ Введите новый текст")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        # Сохраняем индекс для редактирования
        user_analysis[user_id]['edit_index'] = index
        
        msg = bot.send_message(call.message.chat.id, "✏️ Введите новый текст для этой плашки:")
        bot.register_next_step_handler(msg, process_edit_gpt2, call.message.chat.id, user_id)

def process_edit_gpt2(message, chat_id, user_id):
    new_text = message.text.strip()[:40]
    index = user_analysis[user_id].get('edit_index', 0)
    
    # Обновляем текст
    user_analysis[user_id]['gpt2_results'][index]['generated'] = new_text
    
    # Переходим к следующему
    user_analysis[user_id]['edit_index'] = index + 1
    show_gpt2_result(chat_id, user_id, index + 1)

def generate_final_cards(chat_id, user_id):
    bot.send_message(chat_id, "⏳ Формирую итоговые карточки...")
    
    analysis = user_analysis.get(user_id, {})
    gpt2_results = analysis.get('gpt2_results', [])
    title = analysis.get('title', 'ТОВАР')
    style_key = user_data[user_id]['style']
    
    # --- СОБИРАЕМ ПЛАШКИ ---
    all_features = [{"text": item