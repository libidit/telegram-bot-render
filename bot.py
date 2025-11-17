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

if __name__ == '__main__':
    # Удаляем старый webhook и ставим новый
    bot.remove_webhook()
    import time
    time.sleep(1.5)
    
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    bot.set_webhook(url=webhook_url)
    print(f"Webhook установлен: {webhook_url}")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
