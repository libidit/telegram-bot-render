import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, abort, jsonify

import gspread

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

# Ожидаемые переменные окружения (на Render задать в Settings -> Environment)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')           # <--- ваш токен (на render как SECRET)
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')           # <--- id таблицы (из ссылки)
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON')     # <--- содержимое service account JSON (строка)
# Опционально: путь/имя листа
SHEET_NAME = os.environ.get('SHEET_NAME', 'Sheet1')

if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN не задан в окружении")
    raise RuntimeError("TELEGRAM_TOKEN required")
if not SPREADSHEET_ID:
    log.error("SPREADSHEET_ID не задан в окружении")
    raise RuntimeError("SPREADSHEET_ID required")
if not GOOGLE_CREDS_JSON:
    log.error("GOOGLE_CREDS_JSON не задан в окружении")
    raise RuntimeError("GOOGLE_CREDS_JSON required")

# Подключение к Google Sheets через gspread
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        worksheet = sh.worksheet(SHEET_NAME)
    except Exception:
        worksheet = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
    log.info("Connected to Google Sheet")
except Exception as e:
    log.exception("Ошибка подключения к Google Sheets")
    raise

# Flask app
app = Flask(__name__)

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(chat_id, text, reply_to_message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.exception("Ошибка отправки сообщения в Telegram")
        return None

def append_to_sheet(row):
    # Ожидает row как список значений
    try:
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        log.info("Row appended to sheet")
    except Exception:
        log.exception("Ошибка при append_row")

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# Вебхук endpoint. В path мы рекомендуем включить токен (для минимальной валидации).
# Пример URL: https://your-service.onrender.com/webhook/<TELEGRAM_TOKEN>
@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    # Быстрая проверка token в URL (чтобы никто не постил произвольные данные)
    if token != TELEGRAM_TOKEN:
        log.warning("Получён вебхук с неверным токеном в пути")
        abort(403)

    if request.headers.get("content-type") != "application/json":
        # Telegram присылает JSON; если другое — пропускаем
        log.warning("Webhook: неверный content-type")
        abort(400)

    update = request.get_json()
    if not update:
        log.warning("Webhook: пустой JSON")
        return jsonify({"ok": True})

    # Обрабатываем только текстовые сообщения из сообщения пользователя
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            # пропускаем другие update types (callback_query и т.п.) пока
            return jsonify({"ok": True})

        chat = message.get("chat", {})
        from_user = message.get("from", {})
        text = message.get("text", "")
        message_id = message.get("message_id")

        chat_id = chat.get("id")
        username = from_user.get("username") or ""
        first_name = from_user.get("first_name") or ""
        last_name = from_user.get("last_name") or ""
        user_id = from_user.get("id")

        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        # Запись в Google Sheets: столбцы (UTC timestamp, chat_id, user_id, username, first_name, last_name, text)
        row = [ts, chat_id, user_id, username, first_name, last_name, text]
        append_to_sheet(row)

        # Ответ пользователю (echo)
        reply_text = f"Вы написали: {text}" if text else "Я получил ваше сообщение (без текста)"
        send_message(chat_id, reply_text, reply_to_message_id=message_id)

    except Exception as e:
        log.exception("Ошибка обработки update")

    # Telegram ждёт HTTP 200 быстро
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Для локального теста можно использовать встроенный сервер (не для продакшена)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)