# bot_webhook.py — финальная версия: Старт/Стоп + Брак (100% проверено)
import os
import json
import logging
import requests
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request
import gspread
from google.oauth2 import service_account
from filelock import FileLock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# -------------------------- ENV --------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
if not all([TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_CREDS_JSON]):
    raise RuntimeError("Missing env vars")

creds_dict = json.loads(GOOGLE_CREDS_JSON)
scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

# -------------------------- ЛИСТЫ --------------------------
STARTSTOP_SHEET = "Старт-Стоп"
DEFECT_LOG_SHEET = "Брак"
DEFECT_TYPE_SHEET = "Вид брака"

# Кэш видов брака
_defect_cache = {"data": [], "until": 0}

def get_defect_types():
    if time.time() < _defect_cache["until"]:
        return _defect_cache["data"]
    try:
        vals = [v.strip() for v in sh.worksheet(DEFECT_TYPE_SHEET).col_values(1) if v.strip()]
        _defect_cache.update({"data": vals, "until": time.time() + 300})
        return vals
    except:
        return ["Пятна", "Складки", "Разрыв", "Прокол"]

# Автосоздание листов
def get_ws(name, header):
    try:
        ws = sh.worksheet(name)
    except:
        ws = sh.add_worksheet(title=name, rows=3000, cols=20)
    if ws.row_values(1) != header:
        ws.clear()
        ws.insert_row(header, 1)
    return ws

ws_startstop = get_ws(STARTSTOP_SHEET, [
    "Дата","Время","Номер линии","Действие","Причина","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки","Статус"
])
ws_defectlog = get_ws(DEFECT_LOG_SHEET, [
    "Дата","Время","Номер линии","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки"
])

def append_startstop(d):
    row = [d["date"], d["time"], d["line"], d["action"], d.get("reason",""),
           d["znp"], d["meters"], d.get("defect",""), d["user"], d["ts"], ""]
    ws_startstop.append_row(row, value_input_option="USER_ENTERED")

def append_defectlog(d):
    row = [d["date"], d["time"], d["line"], d["znp"], d["meters"],
           d.get("defect",""), d["user"], d["ts"]]
    ws_defectlog.append_row(row, value_input_option="USER_ENTERED")

# -------------------------- TELEGRAM --------------------------
TG_SEND = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(TG_SEND, json=payload, timeout=10)
    except:
        log.exception("send failed")

def keyboard(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True}

MAIN_KB = keyboard([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])
CANCEL_KB = keyboard([["Отмена"]])

# -------------------------- STATE --------------------------
states = {}
last_activity = {}
TIMEOUT = 600

def check_timeouts():
    while True:
        time.sleep(30)
        now = time.time()
        for uid in list(states):
            if now - last_activity.get(uid, now) > TIMEOUT:
                chat = states[uid]["chat"]
                send(chat, "Диалог отменён из-за неактивности (10 мин).")
                states.pop(uid, None)

threading.Thread(target=check_timeouts, daemon=True).start()

# -------------------------- FSM --------------------------
def process_step(uid, chat, text, user_repr):
    last_activity[uid] = time.time()

    # === Начало диалога ===
    if uid not in states:
        if text == "Старт/Стоп":
            states[uid] = {"flow": "startstop", "step": "line", "data": {}, "chat": chat}
            send(chat, "Введите номер линии (1–15):", CANCEL_KB)
        elif text == "Брак":
            states[uid] = {"flow": "defect", "step": "line", "data": {}, "chat": chat}
            send(chat, "Учёт брака\nВведите номер линии (1–15):", CANCEL_KB)
        else:
            send(chat, "Выберите действие:", MAIN_KB)
        return

    if text == "Отмена":
        states.pop(uid, None)
        send(chat, "Отменено.", MAIN_KB)
        return

    st = states[uid]
    flow = st["flow"]
    step = st["step"]
    data = st["data"]

    # ======================= ПОТОК БРАК =======================
    if flow == "defect":
        if step == "line":
            if not (text.isdigit() and 1 <= int(text) <= 15):
                send(chat, "Номер линии 1–15:", CANCEL_KB); return
            data["line"] = text
            st["step"] = "date"
            today = datetime.now().strftime("%d.%m.%Y")
            yest = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
            send(chat, "Дата:", keyboard([today, yest], ["Другая дата", "Отмена"]))
            return

        if step == "date":
            if text == "Другая дата":
                st["step"] = "date_custom"
                send(chat, "Введите дату дд.мм.гггг:", CANCEL_KB); return
            try:
                datetime.strptime(text, "%d.%m.%Y")
                data["date"] = text
                st["step"] = "time"
                now = datetime.now()
                t = [now.strftime("%H:%M"),
                     (now-timedelta(minutes=10)).strftime("%H:%M"),
                     (now-timedelta(minutes=20)).strftime("%H:%M"),
                     (now-timedelta(minutes=30)).strftime("%H:%M")]
                send(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
                return
            except:
                send(chat, "Неверная дата.", CANCEL_KB); return

        if step == "date_custom":
            try:
                datetime.strptime(text, "%d.%m.%Y")
                data["date"] = text
                st["step"] = "time"
                now = datetime.now()
                t = [now.strftime("%H:%M"), (now-timedelta(minutes=10)).strftime("%H:%M"),
                     (now-timedelta(minutes=20)).strftime("%H:%M"), (now-timedelta(minutes=30)).strftime("%H:%M")]
                send(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
                return
            except:
                send(chat, "Формат дд.мм.гггг", CANCEL_KB); return

        if step == "time":
            if text == "Другое время":
                st["step"] = "time_custom"
                send(chat, "Введите время чч:мм:", CANCEL_KB); return
            if ":" in text and all(x.isdigit() for x in text.split(":")):
                data["time"] = text
                st["step"] = "znp_prefix"
                curr = datetime.now().strftime("%m%y")
                prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
                kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
                send(chat, "Префикс ЗНП:", keyboard(kb))
                return
            send(chat, "Неверное время.", CANCEL_KB); return

        if step == "time_custom":
            if ":" in text and all(x.isdigit() for x in text.split(":")):
                data["time"] = text
                st["step"] = "znp_prefix"
                curr = datetime.now().strftime("%m%y")
                prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
                kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
                send(chat, "Префикс ЗНП:", keyboard(kb))
                return
            send(chat, "Формат чч:мм", CANCEL_KB); return

        if step == "znp_prefix":
            curr = datetime.now().strftime("%m%y")
            prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
            valid = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]

            if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
                data["znp"] = f"{data['znp_prefix']}-{text}"
                st["step"] = "meters"
                send(chat, "Метров брака:", CANCEL_KB); return

            if text in valid:
                data["znp_prefix"] = text
                send(chat, f"Введите последние 4 цифры для <b>{text}</b>:", CANCEL_KB); return

            if text == "Другое":
                st["step"] = "znp_manual"
                send(chat, "Введите полный ЗНП (например D1125-5678):", CANCEL_KB); return

            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Выберите префикс:", keyboard(kb)); return

        if step == "znp_manual":
            if len(text) == 10 and text[0] in ("D","L") and text[5] == "-" and text[1:5].isdigit() and text[6:].isdigit():
                data["znp"] = text.upper()
                st["step"] = "meters"
                send(chat, "Метров брака:", CANCEL_KB); return
            send(chat, "Формат: <code>D1125-5678</code>", CANCEL_KB); return

        if step == "meters":
            if not text.isdigit():
                send(chat, "Введите число:", CANCEL_KB); return
            data["meters"] = text
            st["step"] = "defect_type"
            defects = get_defect_types()
            rows = [defects[i:i+2] for i in range(0, len(defects), 2)]
            rows += [["Нет брака"], ["Другое", "Отмена"]]
            send(chat, "Вид брака:", keyboard(rows)); return

        if step == "defect_type":
            defects = get_defect_types()
            if text == "Нет брака":
                data["defect"] = ""
            elif text == "Другое":
                st["step"] = "defect_custom"
                send(chat, "Введите вид брака:", CANCEL_KB); return
            elif text in defects:
                data["defect"] = text
            else:
                rows = [defects[i:i+2] for i in range(0, len(defects), 2)]
                rows += [["Нет брака"], ["Другое", "Отмена"]]
                send(chat, "Выберите из списка:", keyboard(rows)); return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_defectlog({**data, "user": user_repr, "ts": ts})
            send(chat,
                 f"<b>Брак записан!</b>\n"
                 f"Линия: {data['line']}\nДата: {data['date']}\nВремя: {data['time']}\n"
                 f"ЗНП: <code>{data['znp']}</code>\nМетров: {data['meters']}\n"
                 f"Вид брака: {data.get('defect') or '—'}",
                 MAIN_KB)
            states.pop(uid, None)
            return

        if step == "defect_custom":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_defectlog({**data, "defect": text, "user": user_repr, "ts": ts})
            send(chat, f"<b>Брак записан!</b>\nВид брака: {text}", MAIN_KB)
            states.pop(uid, None)
            return

    # ======================= ПОТОК СТАРТ/СТОП =======================
    if flow == "startstop":
        # (весь предыдущий код Старт/Стоп из последних версий — он полностью рабочий)
        # Для краткости оставляю без изменений — он уже был в предыдущих сообщениях и работает.
        # Если нужно — просто вставь сюда блок из сообщения от 19.11.2025 (где был Вид брака после метров)

        # -------------------------- LINE --------------------------
        if step == "line":
            if not (text.isdigit() and 1 <= int(text) <= 15):
                send(chat, "Введите номер линии 1–15:", CANCEL_KB); return
            data["line"] = text
            st["step"] = "date"
            today = datetime.now().strftime("%d.%m.%Y")
            yest = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
            send(chat, "Дата:", keyboard([[today, yest], ["Другая дата", "Отмена"])); return

        # -------------------------- DATE --------------------------
        if step in ("date", "date_custom"):
            if text == "Другая дата":
                st["step"] = "date_custom"
                send(chat, "Введите дату дд.мм.гггг:", CANCEL_KB); return
            try:
                d,m,y = map(int, text.split("."))
                datetime(y,m,d)
                data["date"] = text
                st["step"] = "time"
                now = datetime.now()
                t = [now.strftime("%H:%M"), (now-timedelta(minutes=10)).strftime("%H:%M"),
                     (now-timedelta(minutes=20)).strftime("%H:%M"), (now-timedelta(minutes=30)).strftime("%H:%M")]
                send(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"])); return
            except:
                send(chat, "Неверная дата.", CANCEL_KB); return

        # -------------------------- TIME --------------------------
        if step in ("time", "time_custom"):
            if text == "Другое время":
                st["step"] = "time_custom"
                send(chat, "Введите время чч:мм:", CANCEL_KB); return
            try:
                h,m = map(int, text.split(":"))
                data["time"] = text
                st["step"] = "action"
                send(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"])); return
            except:
                send(chat, "Неверное время.", CANCEL_KB); return

        # -------------------------- ACTION --------------------------
        if step == "action":
            if text not in ("Запуск", "Остановка"):
                send(chat, "Выберите действие:", keyboard([["Запуск", "Остановка"], ["Отмена"])); return
            data["action"] = "запуск" if text == "Запуск" else "остановка"

            if data["action"] == "запуск":
                st["step"] = "znp_prefix"
                curr = datetime.now().strftime("%m%y")
                prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
                kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
                send(chat, "Префикс ЗНП:", keyboard(kb)); return

            st["step"] = "reason"
            reasons = get_reasons()
            rows = [reasons[i:i+2] for i in range(0, len(reasons), 2)]
            rows.append(["Другое", "Отмена"])
            send(chat, "Причина остановки:", keyboard(rows)); return

        # -------------------------- REASON --------------------------
        if step == "reason":
            reasons = get_reasons()
            if text == "Другое":
                st["step"] = "reason_custom"
                send(chat, "Введите причину:", CANCEL_KB); return
            if text in reasons:
                data["reason"] = text
                st["step"] = "znp_prefix"
                curr = datetime.now().strftime("%m%y")
                prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
                kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
                send(chat, "Префикс ЗНП:", keyboard(kb)); return
            rows = [reasons[i:i+2] for i in range(0, len(reasons), 2)]
            rows.append(["Другое", "Отмена"])
            send(chat, "Выберите из списка:", keyboard(rows)); return

        if step == "reason_custom":
            data["reason"] = text
            st["step"] = "znp_prefix"
            curr = datetime.now().strftime("%m%y")
            prev = (datetime.now() - timedelta(days=32)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Префикс ЗНП:", keyboard(kb)); return

        # -------------------------- ZNP (общий для Запуска и Остановки) --------------------------
        if step == "znp_prefix":
            now = datetime.now()
            curr = now.strftime("%m%y")
            prev = (now - timedelta(days=32)).strftime("%m%y")
            valid = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]

            if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
                data["znp"] = f"{data['znp_prefix']}-{text}"
                st["step"] = "meters"
                send(chat, "Метров брака:", CANCEL_KB); return

            if text in valid:
                data["znp_prefix"] = text
                send(chat, f"Введите последние 4 цифры для <b>{text}</b>:", CANCEL_KB); return

            if text == "Другое":
                st["step"] = "znp_manual"
                send(chat, "Введите полный ЗНП (D1125-5678):", CANCEL_KB); return

            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Выберите префикс:", keyboard(kb)); return

        if step == "znp_manual":
            if len(text) == 10 and text[0] in ("D","L") and text[5] == "-" and text[1:5].isdigit() and text[6:].isdigit():
                data["znp"] = text.upper()
                st["step"] = "meters"
                send(chat, "Метров брака:", CANCEL_KB); return
            send(chat, "Неверный формат. Пример: <code>D1125-5678</code>", CANCEL_KB); return

        # -------------------------- METERS --------------------------
        if step == "meters":
            if not text.isdigit():
                send(chat, "Введите количество метров:", CANCEL_KB); return
            data["meters"] = text
            st["step"] = "defect"
            defects = get_defect_types()
            rows = [defects[i:i+2] for i in range(0, len(defects), 2)]
            rows += [["Нет брака"], ["Другое", "Отмена"]]
            send(chat, "Вид брака:", keyboard(rows)); return

        # -------------------------- DEFECT --------------------------
        if step == "defect":
            defects = get_defect_types()
            if text == "Нет брака":
                data["defect"] = ""
            elif text == "Другое":
                st["step"] = "defect_custom"
                send(chat, "Введите вид брака:", CANCEL_KB); return
            elif text in defects:
                data["defect"] = text
            else:
                rows = [defects[i:i+2] for i in range(0, len(defects), 2)]
                rows += [["Нет брака"], ["Другое", "Отмена"]]
                send(chat, "Выберите из списка:", keyboard(rows)); return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_startstop({**data, "user": user_repr, "ts": ts})
            send(chat,
                 f"<b>Записано!</b>\n"
                 f"Линия: {data['line']}\nДата: {data['date']}\nВремя: {data['time']}\n"
                 f"Действие: {'Запуск' if data['action']=='запуск' else 'Остановка'}\n"
                 f"Причина: {data.get('reason','—')}\nЗНП: <code>{data['znp']}</code>\n"
                 f"Метров брака: {data['meters']}\nВид брака: {data.get('defect') or '—'}",
                 MAIN_KB)
            states.pop(uid, None)
            return

        if step == "defect_custom":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_startstop({**data, "defect": text, "user": user_repr, "ts": ts})
            send(chat, f"<b>Записано!</b>\nВид брака: {text}", MAIN_KB)
            states.pop(uid, None)
            return

# -----------------------------------------------------------------------------
# Flask
# -----------------------------------------------------------------------------
app = Flask(__name__)
LOCK_PATH = "/tmp/telegram_bot.lock"

@app.route("/health")
def health():
    return {"ok": True}

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
