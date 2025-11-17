import telebot
from flask import Flask, request
import os
import threading
import time

# ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
TOKEN = "8116386232:AAEj8J_3oaFazKpONtB9PcpmTxjzAIven9w"   # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
# ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←

bot = telebot.TeleBot(TOKEN)

# ===================== ТВОИ ХЕНДЛЕРЫ =====================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Я теперь живу на Render 24/7 🚀")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

# ===================== FLASK =====================
app = Flask(__name__)

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'OK', 403

@app.route('/')
def index():
    return '<h1>Telegram бот работает на Render!</h1>'

# ===================== УСТАНОВКА WEBHOOK ПРИ СТАРТЕ =====================
def setup_webhook():
    time.sleep(3)  # даём gunicorn полностью подняться
    bot.remove_webhook()
    time.sleep(1)
    
    url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{TOKEN}"
    result = bot.set_webhook(url=url)
    if result:
        print(f"Webhook успешно установлен: {url}")
    else:
        print("ОШИБКА установки webhook!")

# Запускаем в отдельном потоке — это работает и под gunicorn
threading.Thread(target=setup_webhook, daemon=True).start()

# Запускаем в отдельном потоке — это работает и под gunicorn
threading.Thread(target=setup_webhook, daemon=True).start()

# Это нужно для Render (gunicorn ищет именно такую переменную)
application = app

# Больше ничего не нужно! Никакого if __name__ == '__main__'
