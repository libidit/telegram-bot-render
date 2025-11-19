# bot_webhook.py — финальная версия с:
# • причинами из листа "Причина остановки"
# • ZNP: D1125-5678 (с дефисом)
# • 4 кнопки префикса + Другое
# • работает на Render.com

import os
import json
import logging
import requests
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import gspread
from google.oauth2 import service_account
from filelock import FileLock

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# -----------------------------------------------------------------------------
# Environment variables
# -----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")

if not all([TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_CREDS_JSON]):
    raise RuntimeError("Missing required env vars")

creds_dict = json.loads(GOOGLE_CREDS_JSON)
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)

# -----------------------------------------------------------------------------
# Google Sheets
# -----------------------------------------------------------------------------
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

STARTSTOP_SHEET_NAME = "Старт-Стоп"
REASONS_SHEET_NAME = "Причина остановки"

# Кэшируем причины на 5 минут (чтобы не дёргать таблицу каждый раз)
_reasons_cache = {"data": [], "until": 0}

def get_reasons():
    """Получить список причин из листа 'Причина остановки', столбец A"""
    global _reasons_cache
    now = time.time()
    if now < _reasons_cache["until"]:
        return _reasons_cache["data"]

    try:
        ws = sh.worksheet(REASONS_SHEET_NAME)
        values = ws.col_values(1)  # столбец A
        reasons = [r.strip() for r in values if r.strip()]
        _reasons_cache = {"data": reasons, "until": now + 300}  # кэш 5 минут
        return reasons
    except Exception as e:
        log.exception("Не удалось загрузить причины остановки")
        return ["Неисправность", "Переналадка", "Нет заготовки", "Другое"]

def get_ws():
    try:
        ws = sh.worksheet(STARTSTOP_SHEET_NAME)
    except:
        ws = sh.add_worksheet(STARTSTOP_SHEET_NAME, rows=2000, cols=20)
    first = ws.row_values(1)
    if first != ["Дата", "Время", "Номер линии", "Действие", "Причина", "ЗНП", "Метров брака", "Пользователь", "Время отправки", "Статус"]:
        ws.clear()
        ws.insert_row(["Дата", "Время", "Номер линии", "Действие", "Причина", "ЗНП", "Метров брака", "Пользователь", "Время отправки", "Статус"], 1)
    return ws

ws = get_ws()

def append_row(d):
    row = [
        d["date"], d["time"], d["line"], d["action"],
        d.get("reason", ""), d["znp"], d["meters"],
        d["user"], d["ts"], ""
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------
TG_SEND = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(TG_SEND, json=payload, timeout=10)
    except Exception:
        log.exception("Failed to send message")

def keyboard(rows):
    return {"keyboard": [[{"text": txt} for txt in row] for row in rows], "resize_keyboard": True}

MAIN_KB = keyboard([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])
CANCEL_KB = keyboard([["Отмена"]])

# -----------------------------------------------------------------------------
# State & timeout
# -----------------------------------------------------------------------------
states = {}
last_activity = {}
TIMEOUT = 600

def check_timeouts():
    while True:
        time.sleep(30)
        now = time.time()
        for uid in list(states.keys()):
            if now - last_activity.get(uid, now) > TIMEOUT:
                chat = states[uid]["chat"]
                send(chat, "Диалог завершён из-за отсутствия активности (10 минут).")
                states.pop(uid, None)

threading.Thread(target=check_timeouts, daemon=True).start()

# -----------------------------------------------------------------------------
# Main FSM
# -----------------------------------------------------------------------------
def process_step(uid, chat, text, user_repr):
    last_activity[uid] = time.time()

    if uid not in states:
        if text == "Старт/Стоп":
            states[uid] = {"step": "line", "data": {}, "chat": chat}
            send(chat, "Введите номер линии (1–15):", CANCEL_KB)
            return
        send(chat, "Выберите действие:", MAIN_KB)
        return

    if text == "Отмена":
        states.pop(uid, None)
        send(chat, "Отменено.", MAIN_KB)
        return

    st = states[uid]
    step = st["step"]
    data = st["data"]

    # -------------------------- LINE --------------------------
    if step == "line":
        if not (text.isdigit() and 1 <= int(text) <= 15):
            send(chat, "Введите номер линии 1–15:", CANCEL_KB)
            return
        data["line"] = text
        st["step"] = "date"
        today = datetime.now().strftime("%d.%m.%Y")
        yest = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
        send(chat, "Дата:", keyboard([[today, yest], ["Другая дата", "Отмена"]]))
        return

    # -------------------------- DATE --------------------------
    if step in ("date", "date_custom"):
        if text == "Другая дата":
            st["step"] = "date_custom"
            send(chat, "Введите дату в формате дд.мм.гггг:", CANCEL_KB)
            return
        try:
            d, m, y = map(int, text.split("."))
            datetime(y, m, d)
            data["date"] = text
            st["step"] = "time"
            now = datetime.now()
            times = [now.strftime("%H:%M"), (now - timedelta(minutes=10)).strftime("%H:%M"),
                     (now - timedelta(minutes=20)).strftime("%H:%M"), (now - timedelta(minutes=30)).strftime("%H:%M")]
            send(chat, "Время:", keyboard([[times[0], times[1], "Другое время"], [times[2], times[3], "Отмена"]]))
            return
        except:
            send(chat, "Неверный формат даты.", CANCEL_KB)
            return

    # -------------------------- TIME --------------------------
    if step in ("time", "time_custom"):
        if text == "Другое время":
            st["step"] = "time_custom"
            send(chat, "Введите время чч:мм:", CANCEL_KB)
            return
        try:
            h, m = map(int, text.split(":"))
            data["time"] = text
            st["step"] = "action"
            send(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
            return
        except:
            send(chat, "Неверный формат времени.", CANCEL_KB)
            return

    # -------------------------- ACTION --------------------------
    if step == "action":
        if text not in ("Запуск", "Остановка"):
            send(chat, "Выберите действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
            return
        data["action"] = "запуск" if text == "Запуск" else "остановка"

        if data["action"] == "запуск":
            st["step"] = "znp_prefix"
            curr = datetime.now().strftime("%m%y")
            prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Выберите префикс ЗНП:", keyboard(kb))
            return

        # Остановка → причины
        st["step"] = "reason"
        reasons = get_reasons()
        rows = [reasons[i:i+2] for i in range(0, len(reasons), 2)]
        rows.append(["Другое", "Отмена"])
        send(chat, "Причина остановки:", keyboard(rows))
        return

    # -------------------------- REASON --------------------------
    if step == "reason":
        reasons = get_reasons()
        if text == "Другое":
            st["step"] = "reason_custom"
            send(chat, "Введите причину остановки:", CANCEL_KB)
            return
        if text in reasons or text == "Отмена":
            data["reason"] = text
            st["step"] = "znp_prefix"
            curr = datetime.now().strftime("%m%y")
            prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Выберите префикс ЗНП:", keyboard(kb))
            return
        # если вдруг не в списке — показываем заново
        rows = [reasons[i:i+2] for i in range(0, len(reasons), 2)]
        rows.append(["Другое", "Отмена"])
        send(chat, "Выберите из списка:", keyboard(rows))
        return

    if step == "reason_custom":
        data["reason"] = text
        st["step"] = "znp_prefix"
        curr = datetime.now().strftime("%m%y")
        prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send(chat, "Выберите префикс ЗНП:", keyboard(kb))
        return

    # -------------------------- ZNP PREFIX --------------------------
    if step == "znp_prefix":
        now = datetime.now()
        curr = now.strftime("%m%y")
        prev = (now - timedelta(days=32)).strftime("%m%y")
        valid_prefixes = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]

        if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
            data["znp"] = f"{data['znp_prefix']}-{text}"
            st["step"] = "meters"
            send(chat, "Метров брака:", CANCEL_KB)
            return

        if text in valid_prefixes:
            data["znp_prefix"] = text
            send(chat, f"Введите последние 4 цифры для <b>{text}</b>:", CANCEL_KB)
            return

        if text == "Другое":
            st["step"] = "znp_full_manual"
            send(chat, "Введите полный ЗНП (например D1125-5678):", CANCEL_KB)
            return

        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send(chat, "Выберите префикс ЗНП:", keyboard(kb))
        return

    # -------------------------- ZNP FULL MANUAL --------------------------
    if step == "znp_full_manual":
        if len(text) == 10 and text[0] in ("D","L") and text[5] == "-" and text[1:5].isdigit() and text[6:].isdigit():
            data["znp"] = text.upper()
            st["step"] = "meters"
            send(chat, "Метров брака:", CANCEL_KB)
            return
        send(chat, "Неверный формат. Пример: <code>D1125-5678</code>", CANCEL_KB)
        return

    # -------------------------- METERS --------------------------
    if step == "meters":
        if not text.isdigit():
            send(chat, "Введите число:", CANCEL_KB)
            return
        data["meters"] = text
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_row({
            "date": data["date"], "time": data["time"], "line": data["line"],
            "action": data["action"], "reason": data.get("reason", ""),
            "znp": data["znp"], "meters": data["meters"],
            "user": user_repr, "ts": ts
        })
        send(chat,
             f"<b>Записано!</b>\n"
             f"Дата: {data['date']}\nВремя: {data['time']}\nЛиния: {data['line']}\n"
             f"Действие: {'Запуск' if data['action']=='запуск' else 'Остановка'}\n"
             f"Причина: {data.get('reason','—')}\nЗНП: <code>{data['znp']}</code>\n"
             f"Метров брака: {data['meters']}",
             MAIN_KB)
        states.pop(uid, None)
        return

# -----------------------------------------------------------------------------
# Flask
# -----------------------------------------------------------------------------
app = Flask(__name__)
LOCK_PATH = "/tmp/telegram_bot.lock"

@app.route("/health")
def health(): return {"ok": True}

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update: return {"ok": True}
    msg = update.get("message")
    if not msg: return {"ok": True}
    chat = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    user_repr = f"{uid} (@{msg['from'].get('username','') or 'без_username'})"

    with FileLock(LOCK_PATH):
        process_step(uid, chat, text, user_repr)
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
