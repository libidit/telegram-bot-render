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

# ----------------------------
# Помощники для secret files
# ----------------------------
def read_secret_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None

SECRETS_DIR = "/etc/secrets"

# ----------------------------
# Чтение конфигурации
# ----------------------------
# Сначала пробуем env vars
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON')  # optional: whole JSON in env
GOOGLE_CREDS_PATH = os.environ.get('GOOGLE_CREDS_PATH')  # optional: path to uploaded secret file
SHEET_NAME = os.environ.get('SHEET_NAME', 'Sheet1')

# Если env vars не заданы, пробуем секретные файлы в /etc/secrets
if not TELEGRAM_TOKEN:
    candidate = os.path.join(SECRETS_DIR, "TELEGRAM_TOKEN")
    token_from_file = read_secret_file(candidate)
    if token_from_file:
        TELEGRAM_TOKEN = token_from_file
        log.info("TELEGRAM_TOKEN загружен из секретного файла")
if not SPREADSHEET_ID:
    candidate = os.path.join(SECRETS_DIR, "SPREADSHEET_ID")
    sid_from_file = read_secret_file(candidate)
    if sid_from_file:
        SPREADSHEET_ID = sid_from_file
        log.info("SPREADSHEET_ID загружен из секретного файла")

# Если GOOGLE_CREDS_PATH не задан, попробуем автоматически найти google_creds.json в /etc/secrets
if not GOOGLE_CREDS_PATH:
    candidate = os.path.join(SECRETS_DIR, "google_creds.json")
    if os.path.exists(candidate):
        GOOGLE_CREDS_PATH = candidate
        log.info("GOOGLE_CREDS_PATH установлен автоматически на /etc/secrets/google_creds.json")

# Если GOOGLE_CREDS_JSON не задан, можно попытаться прочитать secret файл с именем GOOGLE_CREDS_JSON
if not GOOGLE_CREDS_JSON:
    candidate = os.path.join(SECRETS_DIR, "GOOGLE_CREDS_JSON")
    json_from_file = read_secret_file(candidate)
    if json_from_file:
        GOOGLE_CREDS_JSON = json_from_file
        log.info("GOOGLE_CREDS_JSON загружен из секретного файла")

# ----------------------------
# Проверки обязательных значений
# ----------------------------
if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN не задан ни в env, ни в /etc/secrets/TELEGRAM_TOKEN")
    raise RuntimeError("TELEGRAM_TOKEN required")

if not SPREADSHEET_ID:
    log.error("SPREADSHEET_ID не задан ни в env, ни в /etc/secrets/SPREADSHEET_ID")
    raise RuntimeError("SPREADSHEET_ID required")

# ----------------------------
# Инициализация Google Sheets
# ----------------------------
def init_gsheets():
    try:
        if GOOGLE_CREDS_PATH:
            log.info("Использую GOOGLE_CREDS_PATH для подключения к Google Sheets")
            gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)
        elif GOOGLE_CREDS_JSON:
            log.info("Использую GOOGLE_CREDS_JSON из окружения для подключения к Google Sheets")
            creds_dict = json.loads(GOOGLE_CREDS_JSON)
            gc = gspread.service_account_from_dict(creds_dict)
        else:
            # Попытка найти любой .json в /etc/secrets
            if os.path.isdir(SECRETS_DIR):
                for fname in os.listdir(SECRETS_DIR):
                    if fname.lower().endswith(".json"):
                        path = os.path.join(SECRETS_DIR, fname)
                        log.info(f"Найден JSON файл секретов: {path}, попробую использовать его")
                        gc = gspread.service_account(filename=path)
                        sh = gc.open_by_key(SPREADSHEET_ID)
                        try:
                            worksheet = sh.worksheet(SHEET_NAME)
                        except Exception:
                            worksheet = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
                        log.info("Connected to Google Sheet (автоматический JSON из /etc/secrets)")
                        return worksheet
            raise RuntimeError("Нужно задать GOOGLE_CREDS_JSON или GOOGLE_CREDS_PATH или загрузить JSON в /etc/secrets")
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            worksheet = sh.worksheet(SHEET_NAME)
        except Exception:
            worksheet = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
        log.info("Connected to Google Sheet")
        return worksheet
    except Exception as e:
        log.exception("Ошибка подключения к Google Sheets")
        raise

worksheet = init_gsheets()

# ----------------------------
# Flask app и Telegram helpers
# ----------------------------
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
    except Exception:
        log.exception("Ошибка отправки сообщения в Telegram")
        return None

def append_to_sheet(row):
    try:
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        log.info("Row appended to sheet")
    except Exception:
        log.exception("Ошибка при append_row")

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    if token != TELEGRAM_TOKEN:
        log.warning("Получён вебхук с неверным токеном в пути")
        abort(403)
    if request.headers.get("content-type") != "application/json":
        log.warning("Webhook: неверный content-type")
        abort(400)
    update = request.get_json()
    if not update:
        log.warning("Webhook: пустой JSON")
        return jsonify({"ok": True})
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
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
        row = [ts, chat_id, user_id, username, first_name, last_name, text]
        append_to_sheet(row)
        reply_text = f"Вы написали: {text}" if text else "Я получил ваше сообщение (без текста)"
        send_message(chat_id, reply_text, reply_to_message_id=message_id)
    except Exception:
        log.exception("Ошибка обработки update")
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
