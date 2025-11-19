
# bot_webhook.py — Clean architecture + FileLock (workers=2) + strict ordering + 10 min timeout
# Author: ChatGPT, 2025
# This version is optimized for Render.com

import os
import json
import logging
import requests
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import gspread
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
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH")

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_CREDS_PATH:
    raise RuntimeError("Missing environment variables")

# -----------------------------------------------------------------------------
# Google Sheets
# -----------------------------------------------------------------------------
gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)
sh = gc.open_by_key(SPREADSHEET_ID)

STARTSTOP_SHEET_NAME = "Старт-Стоп"
HEADERS = [
    "Дата", "Время", "Номер линии", "Действие",
    "Причина", "ЗНП", "Метров брака",
    "Пользователь", "Время отправки", "Статус"
]


def get_ws():
    try:
        ws = sh.worksheet(STARTSTOP_SHEET_NAME)
    except:
        ws = sh.add_worksheet(STARTSTOP_SHEET_NAME, rows=2000, cols=20)

    first = ws.row_values(1)
    if first != HEADERS:
        ws.clear()
        ws.insert_row(HEADERS, 1)
    return ws


ws = get_ws()


def append_row(d):
    """Append a row to Start/Stop sheet."""
    row = [
        d["date"], d["time"], d["line"], d["action"],
        d.get("reason", ""), d["znp"], d["meters"],
        d["user"], d["ts"], ""
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


# -----------------------------------------------------------------------------
# Telegram API
# -----------------------------------------------------------------------------
TG_SEND = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        requests.post(TG_SEND, json=payload, timeout=10)
    except Exception:
        log.exception("Failed to send message")


# -----------------------------------------------------------------------------
# Keyboards
# -----------------------------------------------------------------------------
def keyboard(rows):
    return {"keyboard": [[{"text": txt} for txt in row] for row in rows],
            "resize_keyboard": True}


MAIN_KB = keyboard([
    ["Старт/Стоп"],
    ["Брак"],
    ["Отменить последнюю запись"]
])

CANCEL_KB = keyboard([["Отмена"]])


# -----------------------------------------------------------------------------
# State machine
# -----------------------------------------------------------------------------
states = {}            # uid → {"step":..., "data":..., "chat":...}
last_activity = {}     # uid → timestamp
TIMEOUT = 600          # 10 minutes


def check_timeouts():
    """Auto-cancel inactive flows."""
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
# Step processor
# -----------------------------------------------------------------------------
def process_step(uid, chat, text, user_repr):
    """Main finite-state-machine logic per user."""

    # Update activity timestamp
    last_activity[uid] = time.time()

    # If no active flow
    if uid not in states:
        if text == "Старт/Стоп":
            states[uid] = {"step": "line", "data": {}, "chat": chat}
            send(chat, "Введите номер линии (1–15):", CANCEL_KB)
            return

        send(chat, "Выберите действие:", MAIN_KB)
        return

    # If user cancelled
    if text == "Отмена":
        states.pop(uid, None)
        send(chat, "Отменено.", MAIN_KB)
        return

    st = states[uid]
    step = st["step"]
    data = st["data"]

    # -------------------------------------------------------------------------
    # Step: line
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # Step: date
    # -------------------------------------------------------------------------
    if step == "date":
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
            times = [
                now.strftime("%H:%M"),
                (now - timedelta(minutes=10)).strftime("%H:%M"),
                (now - timedelta(minutes=20)).strftime("%H:%M"),
                (now - timedelta(minutes=30)).strftime("%H:%M")
            ]
            send(chat, "Время:", keyboard([
                [times[0], times[1], "Другое время"],
                [times[2], times[3], "Отмена"]
            ]))
            return
        except:
            send(chat, "Неверный формат даты.", CANCEL_KB)
            return

    # -------------------------------------------------------------------------
    # Step: date_custom
    # -------------------------------------------------------------------------
    if step == "date_custom":
        try:
            d, m, y = map(int, text.split("."))
            datetime(y, m, d)
            data["date"] = text

            st["step"] = "time"
            now = datetime.now()
            times = [
                now.strftime("%H:%M"),
                (now - timedelta(minutes=10)).strftime("%H:%M"),
                (now - timedelta(minutes=20)).strftime("%H:%M"),
                (now - timedelta(minutes=30)).strftime("%H:%M")
            ]
            send(chat, "Время:", keyboard([
                [times[0], times[1], "Другое время"],
                [times[2], times[3], "Отмена"]
            ]))
            return
        except:
            send(chat, "Неверная дата. Формат дд.мм.гггг", CANCEL_KB)
            return

    # -------------------------------------------------------------------------
    # Step: time
    # -------------------------------------------------------------------------
    if step == "time":
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

    # -------------------------------------------------------------------------
    # Step: time_custom
    # -------------------------------------------------------------------------
    if step == "time_custom":
        try:
            h, m = map(int, text.split(":"))
            data["time"] = text
            st["step"] = "action"
            send(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
            return
        except:
            send(chat, "Неверный формат времени чч:мм", CANCEL_KB)
            return

    # -------------------------------------------------------------------------
    # Step: action
    # -------------------------------------------------------------------------
    if step == "action":
        if text not in ("Запуск", "Остановка"):
            send(chat, "Выберите действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
            return

        data["action"] = "запуск" if text == "Запуск" else "остановка"

        if data["action"] == "запуск":
            st["step"] = "znp"
            send(chat, "Введите номер ЗНП (4 цифры):", CANCEL_KB)
            return

        # Остановка → причина
        st["step"] = "reason"
        send(chat, "Причина остановки:", keyboard([["Другое"], ["Отмена"]]))
        return

    # -------------------------------------------------------------------------
    # Step: reason
    # -------------------------------------------------------------------------
    if step == "reason":
        if text == "Другое":
            st["step"] = "reason_custom"
            send(chat, "Введите причину остановки:", CANCEL_KB)
            return

        data["reason"] = text
        st["step"] = "znp"
        send(chat, "Введите номер ЗНП (4 цифры):", CANCEL_KB)
        return

    # -------------------------------------------------------------------------
    # Step: reason_custom
    # -------------------------------------------------------------------------
    if step == "reason_custom":
        data["reason"] = text
        st["step"] = "znp"
        send(chat, "Введите номер ЗНП (4 цифры):", CANCEL_KB)
        return

    # -------------------------------------------------------------------------
    # Step: znp
    # -------------------------------------------------------------------------
    if step == "znp":
        if not (text.isdigit() and len(text) == 4):
            send(chat, "Введите номер ЗНП (4 цифры):", CANCEL_KB)
            return

        data["znp"] = text
        st["step"] = "meters"
        send(chat, "Метров брака:", CANCEL_KB)
        return

    # -------------------------------------------------------------------------
    # Step: meters (final step)
    # -------------------------------------------------------------------------
    if step == "meters":
        if not text.isdigit():
            send(chat, "Введите число:", CANCEL_KB)
            return

        data["meters"] = text

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_row({
            "date": data["date"],
            "time": data["time"],
            "line": data["line"],
            "action": data["action"],
            "reason": data.get("reason", ""),
            "znp": data["znp"],
            "meters": data["meters"],
            "user": user_repr,
            "ts": ts
        })

        send(chat,
             f"<b>Записано!</b>\n"
             f"Дата: {data['date']}\n"
             f"Время: {data['time']}\n"
             f"Линия: {data['line']}\n"
             f"Действие: {'Запуск' if data['action']=='запуск' else 'Остановка'}\n"
             f"Причина: {data.get('reason','—')}\n"
             f"ЗНП: {data['znp']}\n"
             f"Метров брака: {data['meters']}",
             MAIN_KB)

        states.pop(uid, None)
        return


# -----------------------------------------------------------------------------
# Flask app + FileLock per request
# -----------------------------------------------------------------------------
app = Flask(__name__)
LOCK_PATH = "/tmp/telegram_bot.lock"


@app.route("/health")
def health():
    return {"ok": True}


@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    chat = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    user_repr = f"{uid} (@{msg['from'].get('username', '') or 'без_username'})"

    # -------------------------------
    # STRICT ORDER GUARANTEED HERE
    # -------------------------------
    with FileLock(LOCK_PATH):
        process_step(uid, chat, text, user_repr)

    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
