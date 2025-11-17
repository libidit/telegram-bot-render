import telebot
from flask import Flask, request
import os

TOKEN = "8116386232:AAEj8J_3oaFazKpONtB9PcpmTxjzAIven9w"  # ← СЮДА СВОЙ ТОКЕН

bot = telebot.TeleBot(TOKEN)

# ========== ТВОИ ХЕНДЛЕРЫ ЗДЕСЬ ==========
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Я теперь живу на Render 🚀\nРаботаю 24/7 без polling и ошибок 409 :)")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

# ================= WEBHOOK =================
app = Flask(__name__)

@app.route('/' + TOKEN, methods=['POST'])
def get_message():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return '<h1>Бот работает!</h1>', 200

@app.route('/')
def index():
    return '<h1>Telegram бот на Render</h1><p>Всё ок!</p>'

# === Установка webhook при старте (работает и под gunicorn) ===
import threading

def set_webhook():
    import time
    time.sleep(2)  # даём gunicorn время подняться
    bot.remove_webhook()
    time.sleep(1)
    
    url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{TOKEN}"
    bot.set_webhook(url=url)
    print(f"Webhook успешно установлен: {url}")

# Запускаем установку webhook в отдельном потоке, чтобы не блокировать основной
threading.Thread(target=set_webhook, daemon=True).start()

# ================= Flask routes =================
@app.route('/' + TOKEN, methods=['POST'])
def get_message():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'OK', 403

@app.route('/')
def index():
    return '<h1>Бот работает на Render!</h1>', 200
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
