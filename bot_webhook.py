# bot_webhook.py — Финальная версия: отмена через статус "Удалено" + без ошибок
import os
import json
import logging
import requests
import threading
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import gspread
from google.oauth2 import service_account
from filelock import FileLock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ==================== ENV ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
if not all([TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_CREDS_JSON]):
    raise RuntimeError("Missing required env vars")

creds_dict = json.loads(GOOGLE_CREDS_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
)

gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

# ==================== Московское время ====================
MSK = timezone(timedelta(hours=3))
def now_msk():
    return datetime.now(MSK)

# ==================== Листы ====================
STARTSTOP_SHEET = "Старт-Стоп"
DEFECT_SHEET = "Брак"

HEADERS_STARTSTOP = ["Дата","Время","Номер линии","Действие","Причина","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки","Статус"]
HEADERS_DEFECT = ["Дата","Время","Номер линии","Действие","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки","Статус"]

def get_ws(sheet_name, headers):
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=3000, cols=20)
    if ws.row_values(1) != headers:
        ws.clear()
        ws.insert_row(headers, 1)
    return ws

ws_startstop = get_ws(STARTSTOP_SHEET, HEADERS_STARTSTOP)
ws_defect = get_ws(DEFECT_SHEET, HEADERS_DEFECT)

# ==================== Запись ====================
def append_row(data):
    flow = data.get("flow", "startstop")
    ws = ws_defect if flow == "defect" else ws_startstop
    ts = now_msk().strftime("%Y-%m-%d %H:%M:%S")
    user = data["user"]

    if flow == "defect":
        row = [data["date"], data["time"], data["line"], "брак",
               data.get("znp", ""), data["meters"],
               data.get("defect_type", ""), user, ts, ""]
    else:
        row = [data["date"], data["time"], data["line"], data["action"],
               data.get("reason", ""), data.get("znp", ""), data["meters"],
               data.get("defect_type", ""), user, ts, ""]
    ws.append_row(row, value_input_option="USER_ENTERED")

# ==================== Отмена через статус "Удалено" ====================
def set_delete_status(uid):
    status_col = 11
    ts_col = 10
    user_col = 9

    for ws, name in [(ws_startstop, "Старт-Стоп"), (ws_defect, "Брак")]:
        try:
            all_values = ws.get_all_values()
            if len(all_values) < 2: continue

            latest_row = None
            latest_index = None
            latest_ts = None

            for i in range(len(all_values)-1, 0, -1):
                row = all_values[i]
                if len(row) >= user_col and str(uid) in row[user_col-1]:
                    try:
                        ts_str = row[ts_col-1] if len(row) > ts_col-1 else ""
                        if not ts_str: continue
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        if latest_ts is None or ts > latest_ts:
                            latest_ts = ts
                            latest_row = row
                            latest_index = i + 1
                    except:
                        continue

            if latest_row and latest_index:
                ws.update_cell(latest_index, status_col, "Удалено")
                return True, name, latest_row
        except Exception as e:
            log.error(f"Ошибка при отмене: {e}")
    return False, None, None

# ==================== Клавиатуры ====================
def keyboard(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True, "one_time_keyboard": False}

MAIN_KB = keyboard([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])
CANCEL_KB = keyboard([["Отмена"]])
CONFIRM_KB = keyboard([["Да, удалить", "Нет"]])

# ==================== Динамические клавиатуры ====================
REASONS_CACHE = {"kb": None, "until": 0}
DEFECTS_CACHE = {"kb": None, "until": 0}

def build_kb(sheet_name, extra=None):
    if extra is None: extra = []
    try:
        values = sh.worksheet(sheet_name).col_values(1)[1:]
        items = [v.strip() for v in values if v.strip()] + extra
        rows = [items[i:i+2] for i in range(0, len(items), 2)]
        rows.append(["Отмена"])
        return keyboard(rows)
    except:
        return keyboard([extra[i:i+2] for i in range(0, len(extra), 2)] + [["Отмена"]])

def get_reasons_kb():
    now = time.time()
    if now > REASONS_CACHE["until"]:
        REASONS_CACHE["kb"] = build_kb("Причина остановки", ["Другое"])
        REASONS_CACHE["until"] = now + 300
    return REASONS_CACHE["kb"]

def get_defect_kb():
    now = time.time()
    if now > DEFECTS_CACHE["until"]:
        DEFECTS_CACHE["kb"] = build_kb("Вид брака", ["Другое", "Без брака"])
        DEFECTS_CACHE["until"] = now + 300
    return DEFECTS_CACHE["kb"]

# ==================== Telegram ====================
TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
def send(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    try:
        requests.post(TG, json=payload, timeout=10)
    except Exception as e:
        log.exception(f"send error: {e}")

# ==================== Таймауты ====================
states = {}
last_activity = {}
TIMEOUT = 600

def timeout_worker():
    while True:
        time.sleep(30)
        now = time.time()
        for uid in list(states):
            if now - last_activity.get(uid, now) > TIMEOUT:
                send(states[uid]["chat"], "Диалог прерван — неактивность 10 минут.")
                states.pop(uid, None)
                last_activity.pop(uid, None)

threading.Thread(target=timeout_worker, daemon=True).start()

# ==================== Основная логика ====================
def process(uid, chat, text, user_repr):
    last_activity[uid] = time.time()

    # === Если пользователь в процессе отмены ===
    if uid in states and states[uid].get("step") == "delete_confirm":
        if text == "Да, удалить":
            send(chat, "Запись помечена как <b>Удалено</b> в статусе.", MAIN_KB)
            states.pop(uid, None)
            return
        if text in ("Нет", "Отмена"):
            send(chat, "Отмена отмены. Запись сохранена.", MAIN_KB)
            states.pop(uid, None)
            return

    # === Главное меню ===
    if uid not in states:
        if text in ("/start", "Старт/Стоп"):
            states[uid] = {"step": "line", "data": {}, "chat": chat, "flow": "startstop"}
            send(chat, "Старт/Стоп — учёт простоев\nВведите номер линии (1–15):", CANCEL_KB)
            return
        if text == "Брак":
            states[uid] = {"step": "line", "data": {"action": "брак"}, "chat": chat, "flow": "defect"}
            send(chat, "Учёт брака\nВведите номер линии (1–15):", CANCEL_KB)
            return
        if text == "Отменить последнюю запись":
            success, sheet_name, row = set_delete_status(uid)
            if not success:
                send(chat, "У вас нет записей для отмены.", MAIN_KB)
                return

            # Формируем сообщение
            action = row[3] if len(row) > 3 else "брак"
            znp = row[4] if len(row) > 4 else "—"
            meters = row[5] if len(row) > 5 else "—"
            defect = row[6] if len(row) > 6 else "—"
            ts = row[9] if len(row) > 9 else "—"

            msg = f"<b>Последняя запись (лист '{sheet_name}'):</b>\n"
            msg += f"{row[0]} {row[1]} | Линия {row[2]}\n"
            msg += f"Действие: {action}\n"
            msg += f"ЗНП: <code>{znp}</code>\n"
            msg += f"Брака: {meters} м | {defect}\n"
            msg += f"Отправлено: {ts}\n\n"
            msg += "<b>Удалить эту запись?</b> (статус станет «Удалено»)"
            send(chat, msg, CONFIRM_KB)
            states[uid] = {"step": "delete_confirm", "chat": chat}
            return

        send(chat, "Выберите действие:", MAIN_KB)
        return

    # === Обработка остальных шагов ===
    if text == "Отмена":
        states.pop(uid, None)
        send(chat, "Отменено.", MAIN_KB)
        return

    st = states[uid]
    step = st["step"]
    data = st["data"]
    flow = st.get("flow", "startstop")
    # 1. Линия
    if step == "line":
        if not (text.isdigit() and 1 <= int(text) <= 15):
            send(chat, "Номер линии 1–15:", CANCEL_KB); return
        data["line"] = text
        st["step"] = "date"
        today = now_msk().strftime("%d.%m.%Y")
        yest = (now_msk() - timedelta(days=1)).strftime("%d.%m.%Y")
        send(chat, "Дата:", keyboard([[today, yest], ["Другая дата", "Отмена"]]))
        return

    # 2. Дата
    if step == "date":
        if text == "Другая дата":
            st["step"] = "date_custom"; send(chat, "Введите дату (дд.мм.гггг):", CANCEL_KB); return
        try:
            d, m, y = map(int, text.split("."))
            datetime(y, m, d)
            data["date"] = text
        except:
            send(chat, "Неверная дата.", CANCEL_KB); return
        st["step"] = "time"
        now = now_msk()
        t = [
            now.strftime("%H:%M"),
            (now - timedelta(minutes=10)).strftime("%H:%M"),
            (now - timedelta(minutes=20)).strftime("%H:%M"),
            (now - timedelta(minutes=30)).strftime("%H:%M")
        ]
        send(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
        return

    if step == "date_custom":
        try:
            d, m, y = map(int, text.split("."))
            datetime(y, m, d)
            data["date"] = text
            st["step"] = "time"
            now = now_msk()
            t = [
                now.strftime("%H:%M"),
                (now - timedelta(minutes=10)).strftime("%H:%M"),
                (now - timedelta(minutes=20)).strftime("%H:%M"),
                (now - timedelta(minutes=30)).strftime("%H:%M")
            ]
            send(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
            return
        except:
            send(chat, "Формат дд.мм.гггг", CANCEL_KB); return

    # 3. Время
    if step == "time":
        if text == "Другое время":
            st["step"] = "time_custom"; send(chat, "Введите время (чч:мм):", CANCEL_KB); return
        try:
            h, m = map(int, text.split(":"))
            data["time"] = text
        except:
            send(chat, "Неверное время.", CANCEL_KB); return
        # Для Брака — сразу на ЗНП, без действия
        if flow == "defect":
            st["step"] = "znp_prefix"
            curr = now_msk().strftime("%m%y")
            prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Префикс ЗНП:", keyboard(kb))
        else:
            st["step"] = "action"
            send(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
        return

    if step == "time_custom":
        try:
            h, m = map(int, text.split(":"))
            data["time"] = text
        except:
            send(chat, "Формат чч:мм", CANCEL_KB); return
        if flow == "defect":
            st["step"] = "znp_prefix"
            curr = now_msk().strftime("%m%y")
            prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send(chat, "Префикс ЗНП:", keyboard(kb))
        else:
            st["step"] = "action"
            send(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
        return

    # 4. Действие (только для Старт/Стоп)
    if step == "action":
        if text not in ("Запуск", "Остановка"):
            send(chat, "Выберите действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]])); return
        data["action"] = "запуск" if text == "Запуск" else "остановка"
        if data["action"] == "запуск":
            st["step"] = "znp_prefix"
        else:
            st["step"] = "reason"
            send(chat, "Причина остановки:", get_reasons_kb())
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send(chat, "Префикс ЗНП:", keyboard(kb))
        return

    # 5. Причина (только для остановки)
    if step == "reason":
        if text == "Другое":
            st["step"] = "reason_custom"; send(chat, "Введите причину:", CANCEL_KB); return
        data["reason"] = text
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send(chat, "Префикс ЗНП:", keyboard(kb))
        return

    if step == "reason_custom":
        data["reason"] = text
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send(chat, "Префикс ЗНП:", keyboard(kb))
        return

    # 6. ZNP
    if step == "znp_prefix":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
        valid = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]
        if text in valid:
            data["znp_prefix"] = text
            send(chat, f"Последние 4 цифры для <b>{text}</b>-XXXX:", CANCEL_KB); return
        if text == "Другое":
            st["step"] = "znp_manual"; send(chat, "Полный ЗНП (пример D1125-1234):", CANCEL_KB); return
        if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
            data["znp"] = f"{data['znp_prefix']}-{text}"
            st["step"] = "meters"; send(chat, "Метров брака:", CANCEL_KB); return
        send(chat, "Выберите префикс:", keyboard([[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]))
        return

    if step == "znp_manual":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - timedelta(days=35)).strftime("%m%y")
        if len(text) == 10 and text[5] == "-" and text[:5].upper() in [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]:
            data["znp"] = text.upper()
            st["step"] = "meters"; send(chat, "Метров брака:", CANCEL_KB); return
        send(chat, "Неправильно. Пример: <code>D1125-1234</code>", CANCEL_KB); return

    # 7. Метров брака
    if step == "meters":
        if not text.isdigit():
            send(chat, "Только цифры:", CANCEL_KB); return
        data["meters"] = text
        st["step"] = "defect_type"
        send(chat, "Вид брака:", get_defect_kb())
        return

    # 8. Вид брака → Финал
    if step == "defect_type":
        if text == "Другое":
            st["step"] = "defect_custom"; send(chat, "Опишите вид брака:", CANCEL_KB); return
        data["defect_type"] = "" if text == "Без брака" else text
        data["user"] = user_repr
        data["flow"] = flow
        append_row(data)
        action_text = "Брак" if flow == "defect" else ("Запуск" if data["action"] == "запуск" else "Остановка")
        sheet_name = "Брак" if flow == "defect" else "Старт-Стоп"
        send(chat,
             f"<b>Записано на лист '{sheet_name}'!</b>\n"
             f"Линия: {data['line']} | {data['date']} {data['time']}\n"
             f"Действие: {action_text}\n"
             f"Причина: {data.get('reason','—')}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {data.get('defect_type') or '—'}",
             MAIN_KB)
        states.pop(uid, None)
        return

    if step == "defect_type":
        if text == "Другое":
            st["step"] = "defect_custom"
            send(chat, "Опишите вид брака:", CANCEL_KB)
            return
        data["defect_type"] = "" if text == "Без брака" else text
        data["user"] = user_repr
        data["flow"] = flow
        append_row(data)

        sheet_name = "Брак" if flow == "defect" else "Старт-Стоп"
        action_text = "Брак" if flow == "defect" else ("Запуск" if data.get("action") == "запуск" else "Остановка")

        send(chat,
             f"<b>Записано на лист '{sheet_name}'!</b>\n"
             f"Линия {data['line']} | {data['date']} {data['time']}\n"
             f"Действие: {action_text}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {data.get('defect_type') or '—'}",
             MAIN_KB)
        states.pop(uid, None)
        return

# ==================== Flask ====================
app = Flask(__name__)
LOCK_PATH = "/tmp/bot.lock"

@app.route("/health")
def health(): return {"ok": True}

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    upd = request.get_json(silent=True)
    if not upd or "message" not in upd: return {"ok": True}
    m = upd["message"]
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    text = (m.get("text") or "").strip()
    user_repr = f"{uid} (@{m['from'].get('username','') or 'no_user'})"

    with FileLock(LOCK_PATH):
        process(uid, chat, text, user_repr)
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
