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

# === Надёжная установка webhook на Render (2025 версия) ===
from flask import Flask
import atexit

def final_webhook_setup():
    import time
    time.sleep(4)
    url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    bot.remove_webhook()
    time.sleep(1)
    try:
        bot.set_webhook(url=url, max_connections=100, allowed_updates=[])
        print(f"WEBHOOK УСПЕШНО УСТАНОВЛЕН: {url}")
    except Exception as e:
        print(f"Ошибка установки webhook: {e}")

# Это сработает даже если потоки убиты
atexit.register(final_webhook_setup)

# Дополнительно — попробуем сразу (иногда помогает)
threading.Thread(target=final_webhook_setup, daemon=True).start()

# Запускаем в отдельном потоке — это работает и под gunicorn
threading.Thread(target=setup_webhook, daemon=True).start()

# Это нужно для Render (gunicorn ищет именно такую переменную)
application = app

# Больше ничего не нужно! Никакого if __name__ == '__main__'
