# bot_webhook.py — synchronous version (no executor)
import os
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort, jsonify
import gspread
import threading
from collections import deque

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

# ----------------------------
# Helpers for secret files
# ----------------------------
def read_secret_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None

SECRETS_DIR = "/etc/secrets"

# ----------------------------
# Configuration (ENV or /etc/secrets)
# ----------------------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON')
GOOGLE_CREDS_PATH = os.environ.get('GOOGLE_CREDS_PATH')
STARTSTOP_SHEET_NAME = os.environ.get('STARTSTOP_SHEET_NAME', 'Старт-Стоп')

if not TELEGRAM_TOKEN:
    p = os.path.join(SECRETS_DIR, "TELEGRAM_TOKEN")
    TELEGRAM_TOKEN = read_secret_file(p)

if not SPREADSHEET_ID:
    p = os.path.join(SECRETS_DIR, "SPREADSHEET_ID")
    SPREADSHEET_ID = read_secret_file(p)

if not GOOGLE_CREDS_PATH:
    candidate = os.path.join(SECRETS_DIR, "google_creds.json")
    if os.path.exists(candidate):
        GOOGLE_CREDS_PATH = candidate

if not GOOGLE_CREDS_JSON:
    p = os.path.join(SECRETS_DIR, "GOOGLE_CREDS_JSON")
    GOOGLE_CREDS_JSON = read_secret_file(p)

if not TELEGRAM_TOKEN:
    raise RuntimeError("No TELEGRAM_TOKEN")

if not SPREADSHEET_ID:
    raise RuntimeError("No SPREADSHEET_ID")

# ----------------------------
# Google Sheets init
# ----------------------------
def init_gsheets():
    try:
        if GOOGLE_CREDS_PATH:
            gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)
        elif GOOGLE_CREDS_JSON:
            gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS_JSON))
        else:
            raise RuntimeError("GOOGLE_CREDS not found")
        return gc.open_by_key(SPREADSHEET_ID)
    except Exception:
        log.exception("Ошибка подключения к Google Sheets")
        raise

sh = init_gsheets()

# ----------------------------
# Sheet headers
# ----------------------------
HEADERS_STARTSTOP = [
    'Дата', 'Время', 'Номер линии', 'Действие', 'Причина',
    'ЗНП', 'Метров брака', 'Пользователь', 'Время отправки', 'Статус'
]

def get_or_create_ws_by_name(workbook, name):
    try:
        return workbook.worksheet(name)
    except Exception:
        ws = workbook.add_worksheet(title=name, rows=1000, cols=20)
        return ws

def ensure_headers(ws, headers):
    row = ws.row_values(1)
    if row == headers:
        return
    ws.clear()
    ws.insert_row(headers, 1)

startstop_ws = get_or_create_ws_by_name(sh, STARTSTOP_SHEET_NAME)
ensure_headers(startstop_ws, HEADERS_STARTSTOP)

def append_to_startstop_sheet_by_headers(data_dict):
    row = []
    for h in HEADERS_STARTSTOP:
        row.append(data_dict.get({
            'Дата': 'date_display',
            'Время': 'time',
            'Номер линии': 'line',
            'Действие': 'action',
            'Причина': 'reason',
            'ЗНП': 'znp',
            'Метров брака': 'meters',
            'Пользователь': 'user_repr',
            'Время отправки': 'timestamp',
            'Статус': 'status'
        }.get(h, h), ""))
    startstop_ws.append_row(row, value_input_option="USER_ENTERED")

# ----------------------------
# Reasons helper
# ----------------------------
def load_reasons():
    for name in ['Причина остановки', 'Причины', 'Reasons']:
        try:
            ws = sh.worksheet(name)
            vals = ws.col_values(1)
            res = [v.strip() for v in vals if v.strip()]
            if res and res[0].lower().startswith("прич"):
                res = res[1:]
            if not res:
                return ["Другое"]
            if "Другое" not in res:
                res.append("Другое")
            return res
        except:
            pass
    return ["Другое"]

# ----------------------------
# Keyboards
# ----------------------------
def keyboard(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True}

def main_menu_kb():
    return keyboard([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])

def cancel_kb():
    return keyboard([["Отмена"]])

def date_menu_kb():
    t = datetime.now()
    today = t.strftime("%d.%m.%Y")
    yest = (t - timedelta(days=1)).strftime("%d.%m.%Y")
    return keyboard([[today, yest], ["Другая дата", "Отмена"]])

def time_menu_kb():
    now = datetime.now()
    vals = [(now - timedelta(minutes=m)).strftime("%H:%M") for m in [0, 10, 20, 30]]
    return keyboard([vals[:2] + ["Другое время"], vals[2:] + ["Отмена"]])

def action_menu_kb():
    return keyboard([["Запуск", "Остановка"], ["Отмена"]])

def reasons_menu_kb(reasons):
    rows = []
    for i in range(0, len(reasons), 2):
        rows.append(reasons[i:i+2])
    rows.append(["Отмена"])
    return keyboard(rows)

# ----------------------------
# Conversation state
# ----------------------------
user_states = {}
user_locks = {}
global_locks = threading.Lock()

def get_user_lock(uid):
    with global_locks:
        if uid not in user_locks:
            user_locks[uid] = threading.Lock()
        return user_locks[uid]

def start_flow(uid, user, chat):
    user_states[uid] = {"step": "line", "data": {"user": user, "chat": chat}}

def cancel_flow(uid):
    user_states.pop(uid, None)

# ----------------------------
# Validators
# ----------------------------
def parse_date(s):
    try:
        d, m, y = map(int, s.split('.'))
        dt = datetime(y, m, d)
        return dt.strftime("%Y-%m-%d"), s
    except:
        return None

def valid_time(s):
    try:
        h, m = map(int, s.split(":"))
        return 0 <= h < 24 and 0 <= m < 60
    except:
        return False

# ----------------------------
# Telegram send helper
# ----------------------------
app = Flask(__name__)
TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(chat_id, text, kb=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if kb:
        payload["reply_markup"] = json.dumps(kb, ensure_ascii=False)
    try:
        requests.post(f"{TG_URL}/sendMessage", json=payload, timeout=10)
    except Exception:
        log.exception("Ошибка отправки сообщения")

# ----------------------------
# Dedupe
# ----------------------------
processed_ids = deque(maxlen=2000)
processed_ids_set = set()
p_lock = threading.Lock()

def is_dup(uid):
    with p_lock:
        if uid in processed_ids_set:
            return True
        processed_ids.append(uid)
        processed_ids_set.add(uid)
        if len(processed_ids) == processed_ids.maxlen:
            processed_ids_set.clear()
            processed_ids_set.update(processed_ids)
        return False

# ----------------------------
# PROCESS UPDATE — синхронный порядок
# ----------------------------
def process_update(update):
    msg = update.get("message")
    if not msg:
        return

    text = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    username = msg["from"].get("username", "без_username")
    user_repr = f"{uid} (@{username})"

    lock = get_user_lock(uid)
    with lock:

        # If no state we show main menu
        if uid not in user_states:
            if text == "Старт/Стоп":
                start_flow(uid, msg["from"], chat_id)
                send_message(chat_id, "Номер линии (1-15):", cancel_kb())
                return
            elif text == "Брак":
                send_message(chat_id, "Раздел «Брак» пока не реализован.", main_menu_kb())
                return
            elif text == "Отменить последнюю запись":
                send_message(chat_id, "Функция отмены пока не реализована.", main_menu_kb())
                return
            else:
                send_message(chat_id, "Выберите действие:", main_menu_kb())
                return

        # Continue flow
        s = user_states[uid]
        step = s["step"]
        data = s["data"]

        if text == "Отмена":
            cancel_flow(uid)
            send_message(chat_id, "Отменено.", main_menu_kb())
            return

        # ---- line ----
        if step == "line":
            if not text.isdigit() or not (1 <= int(text) <= 15):
                send_message(chat_id, "Введите номер линии 1-15:", cancel_kb())
                return
            data["line"] = text
            s["step"] = "date"
            send_message(chat_id, "Дата (дд.мм.гггг):", date_menu_kb())
            return

        # ---- date ----
        if step == "date":
            if text == "Другая дата":
                s["step"] = "date_custom"
                send_message(chat_id, "Введите дату (дд.мм.гггг):", cancel_kb())
                return
            parsed = parse_date(text)
            if not parsed:
                send_message(chat_id, "Формат даты неверный. Введите дд.мм.гггг:", date_menu_kb())
                return
            data["date_iso"], data["date_display"] = parsed
            s["step"] = "time"
            send_message(chat_id, "Время (чч:мм):", time_menu_kb())
            return

        # ---- date_custom ----
        if step == "date_custom":
            parsed = parse_date(text)
            if not parsed:
                send_message(chat_id, "Формат даты неверный. Введите дд.мм.гггг:", cancel_kb())
                return
            data["date_iso"], data["date_display"] = parsed
            s["step"] = "time"
            send_message(chat_id, "Время:", time_menu_kb())
            return

        # ---- time ----
        if step == "time":
            if text == "Другое время":
                s["step"] = "time_custom"
                send_message(chat_id, "Введите время (чч:мм):", cancel_kb())
                return
            if not valid_time(text):
                send_message(chat_id, "Формат времени неверный. Введите чч:мм:", time_menu_kb())
                return
            data["time"] = text
            s["step"] = "action"
            send_message(chat_id, "Действие:", action_menu_kb())
            return

        # ---- time_custom ----
        if step == "time_custom":
            if not valid_time(text):
                send_message(chat_id, "Неверный формат времени:", cancel_kb())
                return
            data["time"] = text
            s["step"] = "action"
            send_message(chat_id, "Действие:", action_menu_kb())
            return

        # ---- action ----
        if step == "action":
            if text not in ("Запуск", "Остановка"):
                send_message(chat_id, "Выберите действие:", action_menu_kb())
                return
            data["action"] = "запуск" if text == "Запуск" else "остановка"
            if data["action"] == "запуск":
                s["step"] = "znp"
                send_message(chat_id, "Номер ЗНП (4 цифры):", cancel_kb())
                return
            else:
                s["step"] = "reason"
                send_message(chat_id, "Причина остановки:", reasons_menu_kb(load_reasons()))
                return

        # ---- reason ----
        if step == "reason":
            if text == "Другое":
                s["step"] = "reason_custom"
                send_message(chat_id, "Введите причину:", cancel_kb())
                return
            data["reason"] = text
            s["step"] = "znp"
            send_message(chat_id, "Номер ЗНП (4 цифры):", cancel_kb())
            return

        # ---- reason_custom ----
        if step == "reason_custom":
            data["reason"] = text
            s["step"] = "znp"
            send_message(chat_id, "Номер ЗНП:", cancel_kb())
            return

        # ---- znp ----
        if step == "znp":
            if not (text.isdigit() and len(text) == 4):
                send_message(chat_id, "Введите ЗНП (4 цифры):", cancel_kb())
                return
            data["znp"] = text
            s["step"] = "meters"
            send_message(chat_id, "Метров брака:", cancel_kb())
            return

        # ---- meters ----
        if step == "meters":
            if not text.isdigit():
                send_message(chat_id, "Введите число:", cancel_kb())
                return
            data["meters"] = text

            # запись
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data_dict = {
                "date_display": data["date_display"],
                "time": data["time"],
                "line": data["line"],
                "action": data["action"],
                "reason": data.get("reason", ""),
                "znp": data["znp"],
                "meters": data["meters"],
                "user_repr": user_repr,
                "timestamp": ts,
                "status": ""
            }

            append_to_startstop_sheet_by_headers(data_dict)

            msg = (
                "<b>Записано!</b>\n"
                f"<b>Дата:</b> {data_dict['date_display']}\n"
                f"<b>Время:</b> {data_dict['time']}\n"
                f"<b>Линия:</b> {data_dict['line']}\n"
                f"<b>Действие:</b> {'Запуск' if data_dict['action']=='запуск' else 'Остановка'}\n"
                f"<b>Причина:</b> {data_dict['reason'] or '—'}\n"
                f"<b>ЗНП:</b> {data_dict['znp']}\n"
                f"<b>Метров брака:</b> {data_dict['meters']}\n"
                f"<b>Пользователь:</b> {user_repr}"
            )

            cancel_flow(uid)
            send_message(chat_id, msg, main_menu_kb())
            return

        # fallback
        cancel_flow(uid)
        send_message(chat_id, "Ошибка. Начните заново.", main_menu_kb())

# ----------------------------
# Webhook — синхронный вызов
# ----------------------------
@app.route("/health")
def health():
    return {"status": "ok"}

@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    if token != TELEGRAM_TOKEN:
        abort(403)

    update = request.get_json()
    if not update:
        return {"ok": True}

    update_id = update.get("update_id")
    if update_id and is_dup(update_id):
        return {"ok": True}

    # СИНХРОННАЯ обработка — порядок гарантирован
    process_update(update)

    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
