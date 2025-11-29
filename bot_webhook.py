//project
//├── app.py
//├── config.py
//├── requirements.txt
//└── bot/
//    ├── __init__.py
//    ├── telegram.py
//    ├── utils.py
//    ├── sheets.py
//    ├── users.py
//    ├── notify.py
//    ├── keyboards.py
//    ├── states.py
//    ├── dispatcher.py
//    ├── handlers_startstop.py
//    ├── handlers_defect.py
//    └── handlers_delete.py

//app.py
from flask import Flask, request
from filelock import FileLock
from config import TELEGRAM_TOKEN, PORT, LOCK_PATH
from bot.dispatcher import dispatch_update

app = Flask(__name__)

@app.route("/health")
def health():
    return {"ok": True}

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    upd = request.get_json(silent=True)
    if not upd:
        return {"ok": True}
    with FileLock(LOCK_PATH):
        dispatch_update(upd)
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

//config.py
import os
import json
import logging
from datetime import timedelta
from google.oauth2 import service_account

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
PORT = int(os.getenv("PORT", 5000))

//bot/__init__.py
# package marker

//bot/utils.py
from datetime import datetime, timezone
from config import MSK_OFFSET

def now_msk():
    # return timezone-aware datetime in MSK
    return datetime.now(timezone(MSK_OFFSET))

//bot/telegram.py
import json
import requests
from config import TELEGRAM_TOKEN, log

def send_message(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        # reply_markup must be a JSON string
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        log.exception(f"send_message error: {e}")

//bot/sheets.py
import time
import gspread
from config import CREDS, SPREADSHEET_ID, log
from bot.utils import now_msk
from bot.notify import notify_controllers_cached

gc = gspread.authorize(CREDS)
sh = gc.open_by_key(SPREADSHEET_ID)

# sheet names
STARTSTOP_SHEET = "Старт-Стоп"
DEFECT_SHEET = "Брак"
CTRL_STARTSTOP_SHEET = "Контр_Старт-Стоп"
CTRL_DEFECT_SHEET = "Контр_Брак"
USERS_SHEET = "Пользователи"
REASONS_SHEET = "Причина остановки"
DEFECT_TYPES_SHEET = "Вид брака"

HEADERS_STARTSTOP = ["Дата","Время","Номер линии","Действие","Причина","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки","Статус"]
HEADERS_DEFECT    = ["Дата","Время","Номер линии","Действие","ЗНП","Метров брака","Вид брака","Пользователь","Время отправки","Статус"]

def get_ws(sheet_name, headers=None):
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=3000, cols=20)
    if headers and ws.row_values(1) != headers:
        ws.clear()
        ws.insert_row(headers, 1)
    return ws

# cached worksheet objects
ws_startstop = get_ws(STARTSTOP_SHEET, HEADERS_STARTSTOP)
ws_defect = get_ws(DEFECT_SHEET, HEADERS_DEFECT)
ws_ctrl_ss = get_ws(CTRL_STARTSTOP_SHEET)
ws_ctrl_def = get_ws(CTRL_DEFECT_SHEET)
ws_users = get_ws(USERS_SHEET, ["ID", "username", "allowed", "requested_at", "allowed_at"])

# controllers cache helpers
def get_controllers(sheet):
    try:
        ids = sheet.col_values(1)[1:]
        return [int(i.strip()) for i in ids if i.strip().isdigit()]
    except Exception:
        return []

controllers_startstop = get_controllers(ws_ctrl_ss)
controllers_defect = get_controllers(ws_ctrl_def)

# append with retry
def append_with_retry(ws, row, tries=3, delay=1):
    for i in range(tries):
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            log.warning(f"append_with_retry failed (try {i+1}/{tries}): {e}")
            time.sleep(delay)
            delay *= 2
    return False

# high level append (prepares row and notifies)
def append_entry(data):
    """
    data: dict containing keys according to flow:
     - flow: 'startstop' or 'defect'
     - date, time, line, action, reason, znp, meters, defect_type, user
    """
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

    ok = append_with_retry(ws, row)
    if not ok:
        # return False so caller can notify user
        return False

    # notify controllers
    if flow == "defect":
        msg = (f"НОВАЯ ЗАПИСЬ БРАКА\n"
               f"Линия: {data['line']}\n"
               f"{data['date']} {data['time']}\n"
               f"ЗНП: <code>{data.get('znp','—')}</code>\n"
               f"Метров брака: {data['meters']}\n"
               f"Вид брака: {data.get('defect_type','—')}")
        notify_controllers_cached(controllers_defect, msg)
    else:
        action_ru = "Запуск" if data["action"] == "запуск" else "Остановка"
        msg = (f"НОВАЯ ЗАПИСЬ СТАРТ/СТОП\n"
               f"Линия: {data['line']}\n"
               f"{data['date']} {data['time']}\n"
               f"Действие: {action_ru}\n"
               f"Причина: {data.get('reason','—')}")
        notify_controllers_cached(controllers_startstop, msg)

    return True

# get_last_records (excluding "Удалено")
def get_last_records(ws, n=2):
    try:
        values = ws.get_all_values()
        if len(values) <= 1:
            return []
        header = values[0]
        try:
            status_col_index = header.index("Статус")
        except ValueError:
            status_col_index = -1
        valid = []
        for row in reversed(values[1:]):
            if status_col_index == -1 or len(row) <= status_col_index or row[status_col_index].strip() != "Удалено":
                valid.append(row)
                if len(valid) >= n:
                    break
        return list(reversed(valid))
    except Exception as e:
        log.error(f"get_last_records error: {e}")
        return []

# find last entry by user across two sheets
def find_last_entry(uid):
    user_col = 9  # 1-based
    for ws, name in [(ws_startstop, "Старт-Стоп"), (ws_defect, "Брак")]:
        try:
            values = ws.get_all_values()
            for i in range(len(values)-1, 0, -1):
                row = values[i]
                if len(row) >= user_col and str(uid) in row[user_col-1]:
                    # return success, sheet_name, row, ws, row_index
                    return True, name, row, ws, i + 1
        except Exception as e:
            log.error(f"find_last_entry error: {e}")
    return False, None, None, None, None

# mark as deleted
def mark_as_deleted(ws, row_index):
    try:
        ws.update_cell(row_index, 11, "Удалено")
        row = ws.row_values(row_index)
        if len(row) >= 11:
            sheet = ws.title
            if sheet == DEFECT_SHEET:
                msg = (f"ЗАПИСЬ БРАКА УДАЛЕНА\n"
                       f"Линия: {row[2]}\n"
                       f"{row[0]} {row[1]}\n"
                       f"ЗНП: <code>{row[4]}</code>\n"
                       f"Метров: {row[5]}")
                notify_controllers_cached(controllers_defect, msg)
            else:
                action = "Запуск" if row[3] == "запуск" else "Остановка"
                msg = (f"ЗАПИСЬ СТАРТ/СТОП УДАЛЕНА\n"
                       f"Линия: {row[2]}\n"
                       f"{row[0]} {row[1]}\n"
                       f"Действие: {action}\n"
                       f"Причина: {row[4] if len(row)>4 else '—'}")
                notify_controllers_cached(controllers_startstop, msg)
    except Exception as e:
        log.error(f"mark_as_deleted error: {e}")

//bot/notify.py
import requests
from config import TELEGRAM_TOKEN, log

def notify_controllers_cached(ids, message):
    for cid in ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            log.warning(f"notify_controllers error: {e}")

//bot/keyboards.py
import time
from bot.sheets import sh, REASONS_SHEET, DEFECT_TYPES_SHEET

def keyboard(rows):
    return {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Выберите действие"
    }

MAIN_KB = keyboard([
    ["Старт/Стоп", "Брак"],
    ["Отменить последнюю запись"]
])

CANCEL_KB = keyboard([["Отмена"]])
CONFIRM_KB = keyboard([["Да, удалить", "Нет"]])

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
    except Exception:
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

//bot/users.py
from bot.sheets import ws_users
from bot.utils import now_msk

def is_allowed(uid):
    try:
        rows = ws_users.get_all_records()
        for r in rows:
            if str(r.get("ID")) == str(uid) and str(r.get("allowed")).strip() == "1":
                return True
    except Exception:
        pass
    return False

def has_request(uid):
    try:
        rows = ws_users.get_all_records()
        return any(str(r.get("ID")) == str(uid) for r in rows)
    except Exception:
        return False

def request_access(uid, username):
    try:
        ws_users.append_row([uid, username or "", "0", now_msk().strftime("%Y-%m-%d %H:%M:%S"), ""])
        return True
    except Exception:
        return False

//bot/states.py
import time
import threading
from bot.keyboards import MAIN_KB
from bot.telegram import send_message

states = {}
last_activity = {}
TIMEOUT = 600  # 10 minutes

def timeout_worker():
    while True:
        time.sleep(30)
        now = time.time()
        for uid in list(states):
            if now - last_activity.get(uid, now) > TIMEOUT:
                try:
                    send_message(states[uid]["chat"], "Диалог прерван — неактивность 10 минут.", MAIN_KB)
                except:
                    pass
                states.pop(uid, None)
                last_activity.pop(uid, None)

threading.Thread(target=timeout_worker, daemon=True).start()

//bot/dispatcher.py
from bot.telegram import send_message
from bot.keyboards import MAIN_KB
from bot.users import is_allowed, has_request, request_access
from bot.states import states, last_activity
from bot.handlers_startstop import handle_message as handle_startstop
from bot.handlers_defect import handle_message as handle_defect
from bot.handlers_delete import handle_message as handle_delete

def dispatch_update(upd):
    if "message" not in upd:
        return
    m = upd["message"]
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    text = (m.get("text") or "").strip()
    username = m["from"].get("username", "")

    # Authorization
    if not is_allowed(uid):
        if not has_request(uid):
            request_access(uid, username)
            send_message(chat, "🛑 Вы не авторизованы.\nЗапрос на доступ отправлен администратору.")
        else:
            send_message(chat, "⏳ Доступ ещё не подтверждён администратором.")
        return

    # Update last activity for timeouts
    last_activity[uid] = time.time()

    # If user has no state and asked for main menu
    if uid not in states:
        if text in ("/start", "Старт/Стоп"):
            # Start dialog for start/stop
            states[uid] = {"step": "line", "data": {}, "chat": chat, "flow": "startstop"}
            from bot.handlers_startstop import send_last_startstop
            send_last_startstop(chat)
            send_message(chat, "Введите номер линии (1–15):", )
            return
        if text == "Брак":
            states[uid] = {"step": "line", "data": {"action": "брак"}, "chat": chat, "flow": "defect"}
            from bot.handlers_defect import send_last_defect
            send_last_defect(chat)
            send_message(chat, "Введите номер линии (1–15):")
            return
        if text == "Отменить последнюю запись":
            # delegate deletion handler
            handle_delete(uid, chat, text)
            return
        # default: show menu
        send_message(chat, "Выберите действие:", MAIN_KB)
        return

    # If user has state, route to appropriate handler
    st = states.get(uid)
    flow = st.get("flow", "startstop")
    if flow == "startstop":
        handle_startstop(uid, chat, text)
    elif flow == "defect":
        handle_defect(uid, chat, text)
    elif flow == "delete":
        handle_delete(uid, chat, text)
    else:
        send_message(chat, "Неизвестный поток. Начните заново.", MAIN_KB)
        states.pop(uid, None)

//bot/handlers_delete.py
from bot.sheets import find_last_entry, mark_as_deleted
from bot.telegram import send_message
from bot.keyboards import CONFIRM_KB, MAIN_KB
from bot.states import states

def handle_message(uid, chat, text=None):
    # If invoked directly without state (user clicked "Отменить последнюю запись")
    success, sheet_name, row, ws, row_index = find_last_entry(uid)
    if not success:
        send_message(chat, "У вас нет записей для отмены.", MAIN_KB)
        return

    action = row[3] if len(row) > 3 else "брак"
    znp = row[4] if len(row) > 4 else "—"
    meters = row[5] if len(row) > 5 else "—"
    defect = row[6] if len(row) > 6 else "—"
    msg = f"<b>Последняя запись ({sheet_name}):</b>\n"
    msg += f"{row[0]} {row[1]} • Линия {row[2]}\n"
    msg += f"Действие: {action}\n"
    msg += f"ЗНП: <code>{znp}</code>\n"
    msg += f"Брака: {meters}м | {defect}\n\n"
    msg += "<b>Удалить эту запись?</b> (статус → «Удалено»)"

    send_message(chat, msg, CONFIRM_KB)
    states[uid] = {"step": "delete_confirm", "chat": chat, "data": {"ws": ws, "row_index": row_index}}

def handle_confirmation(uid, chat, text):
    st = states.get(uid)
    if not st or st.get("step") != "delete_confirm":
        send_message(chat, "Нет операции на подтверждение.", )
        return

    if text == "Да, удалить":
        ws = st["data"]["ws"]
        row_index = st["data"]["row_index"]
        mark_as_deleted(ws, row_index)
        send_message(chat, "Запись помечена как <b>Удалено</b>.", MAIN_KB)
    else:
        send_message(chat, "Запись сохранена.", MAIN_KB)
    states.pop(uid, None)

# a small router function for delete flow
def handle_message(uid, chat, text=None):
    # if user already in delete_confirm step, treat as confirmation
    from bot.states import states as ST
    st = ST.get(uid)
    if st and st.get("step") == "delete_confirm":
        handle_confirmation(uid, chat, text)
    else:
        # start delete flow
        success, sheet_name, row, ws, row_index = find_last_entry(uid)
        if not success:
            send_message(chat, "У вас нет записей для отмены.", MAIN_KB)
            return
        action = row[3] if len(row) > 3 else "брак"
        znp = row[4] if len(row) > 4 else "—"
        meters = row[5] if len(row) > 5 else "—"
        defect = row[6] if len(row) > 6 else "—"
        msg = f"<b>Последняя запись ({sheet_name}):</b>\n"
        msg += f"{row[0]} {row[1]} • Линия {row[2]}\n"
        msg += f"Действие: {action}\n"
        msg += f"ЗНП: <code>{znp}</code>\n"
        msg += f"Брака: {meters}м | {defect}\n\n"
        msg += "<b>Удалить эту запись?</b> (статус → «Удалено»)"
        send_message(chat, msg, CONFIRM_KB)
        ST[uid] = {"step": "delete_confirm", "chat": chat, "data": {"ws": ws, "row_index": row_index}}

//bot/handlers_startstop.py
from bot.states import states
from bot.telegram import send_message
from bot.keyboards import CANCEL_KB, keyboard, get_reasons_kb
from bot.sheets import append_entry, get_last_records
from bot.utils import now_msk
from bot.keyboards import MAIN_KB

def send_last_startstop(chat):
    from bot.sheets import ws_startstop
    records = get_last_records(ws_startstop, 2)
    msg = "<b>Последние записи Старт/Стоп:</b>\n\n"
    if not records:
        msg += "Нет записей."
    else:
        for r in records:
            action = "Запуск" if r[3] == "запуск" else "Остановка"
            reason = r[4] if len(r) > 4 else "—"
            msg += f"• {r[0]} {r[1]} | Линия {r[2]} | {action} | {reason}\n"
    send_message(chat, msg)

def handle_message(uid, chat, text):
    st = states[uid]
    step = st["step"]
    data = st["data"]

    if text == "Отмена":
        states.pop(uid, None)
        send_message(chat, "Отменено.", MAIN_KB)
        return

    # line step
    if step == "line":
        if not (text.isdigit() and 1 <= int(text) <= 15):
            send_message(chat, "Номер линии 1–15:", CANCEL_KB)
            return
        data["line"] = text
        st["step"] = "date"
        today = now_msk().strftime("%d.%m.%Y")
        yest = (now_msk() - __import__("datetime").timedelta(days=1)).strftime("%d.%m.%Y")
        send_message(chat, "Дата:", keyboard([[today, yest], ["Другая дата", "Отмена"]]))
        return

    if step == "date":
        if text == "Другая дата":
            st["step"] = "date_custom"
            send_message(chat, "дд.мм.гггг:", CANCEL_KB)
            return
        try:
            __import__("datetime").datetime.strptime(text, "%d.%m.%Y")
            data["date"] = text
        except:
            send_message(chat, "Неверная дата.", CANCEL_KB)
            return
        st["step"] = "time"
        now = now_msk()
        t = [now.strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=10)).strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=20)).strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=30)).strftime("%H:%M")]
        send_message(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
        return

    if step == "date_custom":
        try:
            __import__("datetime").datetime.strptime(text, "%d.%m.%Y")
            data["date"] = text
            st["step"] = "time"
            now = now_msk()
            t = [now.strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=10)).strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=20)).strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=30)).strftime("%H:%M")]
            send_message(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
        except:
            send_message(chat, "Формат дд.мм.гггг", CANCEL_KB)
        return

    if step == "time":
        if text == "Другое время":
            st["step"] = "time_custom"
            send_message(chat, "чч:мм:", CANCEL_KB)
            return
        if not (len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit()):
            send_message(chat, "Неверное время.", CANCEL_KB)
            return
        secs = now_msk().strftime(":%S")
        data["time"] = text + secs
        st["step"] = "action"
        send_message(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
        return

    if step == "time_custom":
        if not (len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit()):
            send_message(chat, "Формат чч:мм", CANCEL_KB)
            return
        data["time"] = text
        st["step"] = "action"
        send_message(chat, "Действие:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
        return

    if step == "action":
        if text not in ("Запуск", "Остановка"):
            send_message(chat, "Выберите:", keyboard([["Запуск", "Остановка"], ["Отмена"]]))
            return
        data["action"] = "запуск" if text == "Запуск" else "остановка"
        if data["action"] == "запуск":
            st["step"] = "znp_prefix"
            curr = now_msk().strftime("%m%y")
            prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
            kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
            send_message(chat, "Префикс ЗНП:", keyboard(kb))
        else:
            st["step"] = "reason"
            send_message(chat, "Причина остановки:", get_reasons_kb())
        return

    if step == "reason":
        if text == "Другое":
            st["step"] = "reason_custom"
            send_message(chat, "Введите причину:", CANCEL_KB)
            return
        data["reason"] = text
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send_message(chat, "Префикс ЗНП:", keyboard(kb))
        return

    if step == "reason_custom":
        data["reason"] = text
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send_message(chat, "Префикс ЗНП:", keyboard(kb))
        return

    if step == "znp_prefix":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        valid = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]
        if text in valid:
            data["znp_prefix"] = text
            send_message(chat, f"Последние 4 цифры для <b>{text}</b>-XXXX:", CANCEL_KB)
            return
        if text == "Другое":
            st["step"] = "znp_manual"
            send_message(chat, "Полный ЗНП (D1125-1234):", CANCEL_KB)
            return
        if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
            data["znp"] = f"{data['znp_prefix']}-{text}"
            st["step"] = "meters"
            send_message(chat, "Метров брака:", CANCEL_KB)
            return
        send_message(chat, "Выберите префикс:", keyboard([[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]))
        return

    if step == "znp_manual":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        if len(text) == 10 and text[5] == "-" and text[:5].upper() in [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]:
            data["znp"] = text.upper()
            st["step"] = "meters"
            send_message(chat, "Метров брака:", CANCEL_KB)
            return
        send_message(chat, "Неправильно. Пример: <code>D1125-1234</code>", CANCEL_KB)
        return

    if step == "meters":
        if not text.isdigit():
            send_message(chat, "Только цифры:", CANCEL_KB)
            return
        data["meters"] = text
        st["step"] = "defect_type"
        send_message(chat, "Вид брака:", __import__("bot.keyboards", fromlist=['get_defect_kb']).get_defect_kb())
        return

    if step == "defect_type":
        if text == "Другое":
            st["step"] = "defect_custom"
            send_message(chat, "Опишите вид брака:", CANCEL_KB)
            return
        data["defect_type"] = "" if text == "Без брака" else text
        data["user"] = f"{uid} (user)"
        data["flow"] = "startstop"
        ok = append_entry(data)
        if not ok:
            send_message(chat, "⚠ Ошибка записи в Google Sheets. Попробуйте ещё раз.", MAIN_KB)
            states.pop(uid, None)
            return
        sheet_name = "Старт-Стоп"
        action_text = "Запуск" if data["action"] == "запуск" else "Остановка"
        send_message(chat,
             f"<b>Записано на лист '{sheet_name}'!</b>\n"
             f"Линия {data['line']} • {data['date']} {data['time']}\n"
             f"Действие: {action_text}\n"
             f"Причина: {data.get('reason','—')}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {data.get('defect_type') or '—'}",
             MAIN_KB)
        states.pop(uid, None)
        return

    if step == "defect_custom":
        data["defect_type"] = text
        data["user"] = f"{uid} (user)"
        data["flow"] = "startstop"
        ok = append_entry(data)
        if not ok:
            send_message(chat, "⚠ Ошибка записи в Google Sheets. Попробуйте ещё раз.", MAIN_KB)
            states.pop(uid, None)
            return
        send_message(chat,
             f"<b>Записано на лист 'Старт-Стоп'!</b>\n"
             f"Линия {data['line']} • {data['date']} {data['time']}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {text}", MAIN_KB)
        states.pop(uid, None)
        return

//bot/handlers_defect.py
from bot.states import states
from bot.telegram import send_message
from bot.keyboards import CANCEL_KB, keyboard, get_defect_kb
from bot.sheets import append_entry, get_last_records
from bot.utils import now_msk
from bot.keyboards import MAIN_KB

def send_last_defect(chat):
    from bot.sheets import ws_defect
    records = get_last_records(ws_defect, 2)
    msg = "<b>Последние записи Брака:</b>\n\n"
    if not records:
        msg += "Нет записей."
    else:
        for r in records:
            znp = r[4] if len(r) > 4 else "—"
            meters = r[5] if len(r) > 5 else "—"
            defect = r[6] if len(r) > 6 else "—"
            msg += f"• {r[0]} {r[1]} | Линия {r[2]} | <code>{znp}</code> | {meters}м | {defect}\n"
    send_message(chat, msg)

def handle_message(uid, chat, text):
    st = states[uid]
    step = st["step"]
    data = st["data"]
    flow = "defect"

    if text == "Отмена":
        states.pop(uid, None)
        send_message(chat, "Отменено.", MAIN_KB)
        return

    if step == "line":
        if not (text.isdigit() and 1 <= int(text) <= 15):
            send_message(chat, "Номер линии 1–15:", CANCEL_KB)
            return
        data["line"] = text
        st["step"] = "date"
        today = now_msk().strftime("%d.%m.%Y")
        yest = (now_msk() - __import__("datetime").timedelta(days=1)).strftime("%d.%m.%Y")
        send_message(chat, "Дата:", keyboard([[today, yest], ["Другая дата", "Отмена"]]))
        return

    if step == "date":
        if text == "Другая дата":
            st["step"] = "date_custom"
            send_message(chat, "дд.мм.гггг:", CANCEL_KB)
            return
        try:
            __import__("datetime").datetime.strptime(text, "%d.%m.%Y")
            data["date"] = text
        except:
            send_message(chat, "Неверная дата.", CANCEL_KB)
            return
        st["step"] = "time"
        now = now_msk()
        t = [now.strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=10)).strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=20)).strftime("%H:%M"),
             (now - __import__("datetime").timedelta(minutes=30)).strftime("%H:%M")]
        send_message(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
        return

    if step == "date_custom":
        try:
            __import__("datetime").datetime.strptime(text, "%d.%m.%Y")
            data["date"] = text
            st["step"] = "time"
            now = now_msk()
            t = [now.strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=10)).strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=20)).strftime("%H:%M"),
                 (now - __import__("datetime").timedelta(minutes=30)).strftime("%H:%M")]
            send_message(chat, "Время:", keyboard([[t[0], t[1], "Другое время"], [t[2], t[3], "Отмена"]]))
        except:
            send_message(chat, "Формат дд.мм.гггг", CANCEL_KB)
        return

    if step == "time":
        if text == "Другое время":
            st["step"] = "time_custom"
            send_message(chat, "чч:мм:", CANCEL_KB)
            return
        if not (len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit()):
            send_message(chat, "Неверное время.", CANCEL_KB)
            return
        secs = now_msk().strftime(":%S")
        data["time"] = text + secs
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send_message(chat, "Префикс ЗНП:", keyboard(kb))
        return

    if step == "time_custom":
        if not (len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit()):
            send_message(chat, "Формат чч:мм", CANCEL_KB)
            return
        data["time"] = text
        st["step"] = "znp_prefix"
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        kb = [[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]
        send_message(chat, "Префикс ЗНП:", keyboard(kb))
        return

    if step == "znp_prefix":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        valid = [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]
        if text in valid:
            data["znp_prefix"] = text
            send_message(chat, f"Последние 4 цифры для <b>{text}</b>-XXXX:", CANCEL_KB)
            return
        if text == "Другое":
            st["step"] = "znp_manual"
            send_message(chat, "Полный ЗНП (D1125-1234):", CANCEL_KB)
            return
        if text.isdigit() and len(text) == 4 and "znp_prefix" in data:
            data["znp"] = f"{data['znp_prefix']}-{text}"
            st["step"] = "meters"
            send_message(chat, "Метров брака:", CANCEL_KB)
            return
        send_message(chat, "Выберите префикс:", keyboard([[f"D{curr}", f"L{curr}"], [f"D{prev}", f"L{prev}"], ["Другое", "Отмена"]]))
        return

    if step == "znp_manual":
        curr = now_msk().strftime("%m%y")
        prev = (now_msk() - __import__("datetime").timedelta(days=35)).strftime("%m%y")
        if len(text) == 10 and text[5] == "-" and text[:5].upper() in [f"D{curr}", f"L{curr}", f"D{prev}", f"L{prev}"]:
            data["znp"] = text.upper()
            st["step"] = "meters"
            send_message(chat, "Метров брака:", CANCEL_KB)
            return
        send_message(chat, "Неправильно. Пример: <code>D1125-1234</code>", CANCEL_KB)
        return

    if step == "meters":
        if not text.isdigit():
            send_message(chat, "Только цифры:", CANCEL_KB)
            return
        data["meters"] = text
        st["step"] = "defect_type"
        send_message(chat, "Вид брака:", get_defect_kb())
        return

    if step == "defect_type":
        if text == "Другое":
            st["step"] = "defect_custom"
            send_message(chat, "Опишите вид брака:", CANCEL_KB)
            return
        data["defect_type"] = "" if text == "Без брака" else text
        data["user"] = f"{uid} (user)"
        data["flow"] = "defect"
        ok = append_entry(data)
        if not ok:
            send_message(chat, "⚠ Ошибка записи в Google Sheets. Попробуйте ещё раз.", MAIN_KB)
            states.pop(uid, None)
            return
        send_message(chat,
             f"<b>Записано на лист 'Брак'!</b>\n"
             f"Линия {data['line']} • {data['date']} {data['time']}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {data.get('defect_type') or '—'}",
             MAIN_KB)
        states.pop(uid, None)
        return

    if step == "defect_custom":
        data["defect_type"] = text
        data["user"] = f"{uid} (user)"
        data["flow"] = "defect"
        ok = append_entry(data)
        if not ok:
            send_message(chat, "⚠ Ошибка записи в Google Sheets. Попробуйте ещё раз.", MAIN_KB)
            states.pop(uid, None)
            return
        send_message(chat,
             f"<b>Записано на лист 'Брак'!</b>\n"
             f"Линия {data['line']} • {data['date']} {data['time']}\n"
             f"ЗНП: <code>{data.get('znp','—')}</code>\n"
             f"Брака: {data['meters']} м\n"
             f"Вид брака: {text}",
             MAIN_KB)
        states.pop(uid, None)
        return
