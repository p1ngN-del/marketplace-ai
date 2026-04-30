import os
from flask import Flask, request, jsonify
from huggingface_hub import InferenceClient
import base64
import glob

app = Flask(__name__)

# Получаем токен из переменных окружения (безопасно!)
HF_TOKEN = os.environ.get("HF_TOKEN")

if not HF_TOKEN:
    print("ВНИМАНИЕ: Токен HF_TOKEN не найден!")

client = InferenceClient(token=HF_TOKEN)

@app.route('/analyze', methods=['POST'])
def analyze_image():
    # Проверяем, есть ли файл в запросе
    if 'file' not in request.files:
        return jsonify({"error": "Нет файла"}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"error": "Файл не выбран"}), 400

    try:
        # Читаем файл в память
        image_data = file.read()
        base64_image = base64.b64encode(image_data).decode('utf-8')
        
        # Отправляем в Qwen
        response = client.chat.completions.create(
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        {"type": "text", "text": "Опиши товар для маркетплейса: Название, Цвет, Материал, Преимущества."}
                    ]
                }
            ],
            max_tokens=300
        )
        
        result_text = response.choices[0].message.content
        return jsonify({"description": result_text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Запускаем сервер на порту, который требует Railway
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port)