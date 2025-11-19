import os
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort, jsonify

import gspread

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
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON')  # optional: whole JSON in env
GOOGLE_CREDS_PATH = os.environ.get('GOOGLE_CREDS_PATH')  # optional: path to uploaded secret file
SHEET_NAME = os.environ.get('SHEET_NAME', 'Sheet1')

# Fallback to secret files if env not set (Render mounts uploaded secret files to /etc/secrets)
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

if not GOOGLE_CREDS_PATH:
    candidate = os.path.join(SECRETS_DIR, "google_creds.json")
    if os.path.exists(candidate):
        GOOGLE_CREDS_PATH = candidate
        log.info("GOOGLE_CREDS_PATH установлен автоматически на /etc/secrets/google_creds.json")

if not GOOGLE_CREDS_JSON:
    candidate = os.path.join(SECRETS_DIR, "GOOGLE_CREDS_JSON")
    json_from_file = read_secret_file(candidate)
    if json_from_file:
        GOOGLE_CREDS_JSON = json_from_file
        log.info("GOOGLE_CREDS_JSON загружен из секретного файла")

# ----------------------------
# Validate required config
# ----------------------------
if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN не задан ни в env, ни в /etc/secrets/TELEGRAM_TOKEN")
    raise RuntimeError("TELEGRAM_TOKEN required")
if not SPREADSHEET_ID:
    log.error("SPREADSHEET_ID не задан ни в env, ни в /etc/secrets/SPREADSHEET_ID")
    raise RuntimeError("SPREADSHEET_ID required")

# ----------------------------
# Google Sheets init (supports GOOGLE_CREDS_PATH or GOOGLE_CREDS_JSON)
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
            # попытка найти любой .json в /etc/secrets
            if os.path.isdir(SECRETS_DIR):
                for fname in os.listdir(SECRETS_DIR):
                    if fname.lower().endswith(".json"):
                        path = os.path.join(SECRETS_DIR, fname)
                        log.info(f"Найден JSON файл секретов: {path}, попробую использовать его")
                        gc = gspread.service_account(filename=path)
                        break
                else:
                    raise RuntimeError("Нужно задать GOOGLE_CREDS_JSON или GOOGLE_CREDS_PATH")
            else:
                raise RuntimeError("Нужно задать GOOGLE_CREDS_JSON или GOOGLE_CREDS_PATH")
        sh = gc.open_by_key(SPREADSHEET_ID)
        return sh
    except Exception as e:
        log.exception("Ошибка подключения к Google Sheets")
        raise

sh = init_gsheets()

# ----------------------------
# Helper to get or create worksheet by name (tries a few common names)
# ----------------------------
def get_or_create_ws(workbook, names):
    """
    names: list of possible sheet names in order of preference
    returns first found worksheet or creates workbook.worksheet(names[0]) if none found.
    """
    for name in names:
        try:
            ws = workbook.worksheet(name)
            log.info(f"Использую лист '{name}'")
            return ws
        except Exception:
            continue
    # None found: create first name
    try:
        ws = workbook.add_worksheet(title=names[0], rows=1000, cols=20)
        log.info(f"Создан лист '{names[0]}'")
        return ws
    except Exception:
        # fallback to default sheet
        ws = workbook.sheet1
        log.warning(f"Не удалось создать лист, использую первый лист книги: {ws.title}")
        return ws

# For start/stop we accept variants 'Старт-Стоп' and 'Стар/Стоп' and also SHEET_NAME
STARTSTOP_SHEET_NAMES = [SHEET_NAME, 'Старт-Стоп', 'Стар/Стоп', 'Start-Stop']
startstop_ws = get_or_create_ws(sh, STARTSTOP_SHEET_NAMES)

# For reasons we expect sheet named 'Причина остановки' or 'Причины'
REASONS_SHEET_NAMES = ['Причина остановки', 'Причины', 'Reasons']
# Try to get reasons worksheet, if not found create an empty one
try:
    reasons_ws = get_or_create_ws(sh, REASONS_SHEET_NAMES)
except Exception:
    reasons_ws = None

# ----------------------------
# Flask app and Telegram helpers
# ----------------------------
app = Flask(__name__)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(chat_id, text, reply_to_message_id=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        # reply_markup should be a python dict -> JSON
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("Ошибка отправки сообщения в Telegram")
        return None

def append_to_startstop_sheet(row):
    """
    Append a row to the start/stop worksheet.
    Recommended row format: [date_display, time, line, action, reason, user_repr, timestamp]
    """
    try:
        startstop_ws.append_row(row, value_input_option='USER_ENTERED')
        log.info("Row appended to startstop sheet")
    except Exception:
        log.exception("Ошибка при append_row в startstop sheet")

def load_reasons():
    """
    Load reasons from reasons_ws: read first column from row 2 downwards, skip empty.
    Returns list of reasons (strings). If no worksheet or empty, returns ['Другое'].
    """
    try:
        if not reasons_ws:
            return ['Другое']
        values = reasons_ws.col_values(1)  # column A
        # Skip header if present and blank entries
        res = [v.strip() for v in values if v and v.strip()]
        # If header exists and equals typical header words, remove it (heuristic)
        if res and res[0].lower().startswith('прич') :
            res = res[1:]
        if not res:
            return ['Другое']
        if 'Другое' not in res:
            res.append('Другое')
        return res
    except Exception:
        log.exception("Ошибка при загрузке причин")
        return ['Другое']

# ----------------------------
# Keyboards builders (ReplyKeyboardMarkup)
# ----------------------------
def keyboard_from_rows(rows, one_time=False, resize=True):
    kb = {
        "keyboard": [[{"text": text} for text in row] for row in rows],
        "one_time_keyboard": one_time,
        "resize_keyboard": resize,
    }
    return kb

def main_menu_kb():
    # three buttons, size by content -> one per row
    return keyboard_from_rows([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])

def cancel_kb():
    return keyboard_from_rows([["Отмена"]])

def date_menu_kb():
    today = datetime.now().strftime('%d.%m.%Y')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%d.%m.%Y')
    return keyboard_from_rows([[today, yesterday], ["Другая дата", "Отмена"]])

def time_menu_kb():
    now = datetime.now()
    times = []
    for mins in [0, 10, 20, 30]:
        t = (now - timedelta(minutes=mins)).strftime('%H:%M')
        times.append(t)
    # split into two columns
    row1 = times[:2]
    row2 = times[2:]
    row1.append('Другое время')
    row2.append('Отмена')
    return keyboard_from_rows([row1, row2])

def action_menu_kb():
    return keyboard_from_rows([["Запуск", "Остановка"], ["Отмена"]])

def reasons_menu_kb(reasons_list):
    # build rows of 2 per row (adjust visually)
    rows = []
    r = list(reasons_list)
    # put 'Отмена' as last row
    if 'Другое' not in r:
        r.append('Другое')
    for i in range(0, len(r), 2):
        rows.append(r[i:i+2])
    rows.append(["Отмена"])
    return keyboard_from_rows(rows)

# ----------------------------
# Conversation state
# ----------------------------
# user_states: uid -> dict with keys: flow, step, data
user_states = {}

def start_startstop_flow(uid, user_info, chat_id):
    user_states[uid] = {
        "flow": "startstop",
        "step": "line",
        "data": {
            "user": user_info,
            "chat_id": chat_id
        }
    }

def cancel_flow(uid):
    if uid in user_states:
        del user_states[uid]

# ----------------------------
# Parsers and validators
# ----------------------------
def parse_date_input(s):
    try:
        d, m, y = map(int, s.split('.'))
        # basic validation
        datetime(year=y, month=m, day=d)
        return f"{y:04d}-{m:02d}-{d:02d}", s  # iso, display dd.mm.yyyy
    except Exception:
        return None, None

def validate_time_input(s):
    import re
    if re.match(r'^\d{2}:\d{2}$', s):
        hh, mm = map(int, s.split(':'))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return True
    return False

# ----------------------------
# Flask webhook endpoint
# ----------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    # quick path token check
    if token != TELEGRAM_TOKEN:
        log.warning("Получён вебхук с неверным токеном в пути")
        abort(403)

    if request.headers.get("content-type") != "application/json":
        log.warning("Webhook: неверный content-type")
        abort(400)

    update = request.get_json()
    if not update:
        return jsonify({"ok": True})

    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            # ignore other update types for now
            return jsonify({"ok": True})

        text = message.get("text", "").strip()
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        uid = from_user.get("id")
        username = from_user.get("username") or ""
        user_repr = f"{uid} (@{username or 'без_username'})"

        # If no active conversation -> react to main menu buttons or commands
        if uid not in user_states:
            # Handle main menu interactions
            if text == "Старт/Стоп":
                # start flow
                start_startstop_flow(uid, from_user, chat_id)
                send_message(chat_id, "Номер линии (1-15):", reply_markup=cancel_kb())
                return jsonify({"ok": True})
            elif text == "Брак":
                # Not implemented yet (per request). Reply with stub.
                send_message(chat_id, "Раздел «Брак» пока не реализован.", reply_markup=main_menu_kb())
                return jsonify({"ok": True})
            elif text == "Отменить последнюю запись":
                # Not implemented now — stub
                send_message(chat_id, "Функция отмены пока не реализована.", reply_markup=main_menu_kb())
                return jsonify({"ok": True})
            else:
                # send main menu
                send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
                return jsonify({"ok": True})

        # There is an active conversation
        state = user_states.get(uid)
        if not state or state.get("flow") != "startstop":
            # unexpected, clear and show menu
            cancel_flow(uid)
            send_message(chat_id, "Произошла ошибка состояния. Начните заново.", reply_markup=main_menu_kb())
            return jsonify({"ok": True})

        step = state.get("step")

        # Handle global cancel
        if text == "Отмена":
            cancel_flow(uid)
            send_message(chat_id, "Отменено.", reply_markup=main_menu_kb())
            return jsonify({"ok": True})

        # Step: line
        if step == "line":
            # expect integer 1..15
            if not text.isdigit() or not (1 <= int(text) <= 15):
                send_message(chat_id, "Введите номер линии от 1 до 15 (целое число):", reply_markup=cancel_kb())
                return jsonify({"ok": True})
            state["data"]["line"] = text
            state["step"] = "date"
            # send date options
            send_message(chat_id, "Дата (дд.мм.гггг):", reply_markup=date_menu_kb())
            return jsonify({"ok": True})

        # Step: date
        if step == "date":
            if text == "Другая дата":
                send_message(chat_id, "Введите дату в формате дд.мм.гггг:", reply_markup=cancel_kb())
                state["step"] = "date_custom"
                return jsonify({"ok": True})
            parsed_iso, display = parse_date_input(text)
            if not parsed_iso:
                send_message(chat_id, "Неверный формат даты. Введите в формате дд.мм.гггг или выберите кнопку:", reply_markup=date_menu_kb())
                return jsonify({"ok": True})
            state["data"]["date_iso"] = parsed_iso
            state["data"]["date_display"] = display
            state["step"] = "time"
            send_message(chat_id, "Время (чч:мм):", reply_markup=time_menu_kb())
            return jsonify({"ok": True})

        if step == "date_custom":
            parsed_iso, display = parse_date_input(text)
            if not parsed_iso:
                send_message(chat_id, "Неверный формат. Введите дату в формате дд.мм.гггг:", reply_markup=cancel_kb())
                return jsonify({"ok": True})
            state["data"]["date_iso"] = parsed_iso
            state["data"]["date_display"] = display
            state["step"] = "time"
            send_message(chat_id, "Время (чч:мм):", reply_markup=time_menu_kb())
            return jsonify({"ok": True})

        # Step: time
        if step == "time":
            if text == "Другое время":
                send_message(chat_id, "Введите время в формате чч:мм:", reply_markup=cancel_kb())
                state["step"] = "time_custom"
                return jsonify({"ok": True})
            if not validate_time_input(text):
                send_message(chat_id, "Неверный формат времени. Введите чч:мм или выберите кнопку:", reply_markup=time_menu_kb())
                return jsonify({"ok": True})
            state["data"]["time"] = text
            state["step"] = "action"
            send_message(chat_id, "Действие:", reply_markup=action_menu_kb())
            return jsonify({"ok": True})

        if step == "time_custom":
            if not validate_time_input(text):
                send_message(chat_id, "Неверный формат времени. Введите чч:мм:", reply_markup=cancel_kb())
                return jsonify({"ok": True})
            state["data"]["time"] = text
            state["step"] = "action"
            send_message(chat_id, "Действие:", reply_markup=action_menu_kb())
            return jsonify({"ok": True})

        # Step: action
        if step == "action":
            if text not in ["Запуск", "Остановка"]:
                send_message(chat_id, "Выберите действие: Запуск или Остановка", reply_markup=action_menu_kb())
                return jsonify({"ok": True})
            action = "запуск" if text == "Запуск" else "остановка"
            state["data"]["action"] = action
            if action == "запуск":
                # finish flow: write to sheet and confirm
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                row = [
                    state["data"].get("date_display", ""),
                    state["data"].get("time", ""),
                    state["data"].get("line", ""),
                    action,
                    "",  # reason empty
                    user_repr,
                    ts
                ]
                append_to_startstop_sheet(row)
                # send confirmation
                msg = (
                    "<b>Записано!</b>\n"
                    f"<b>Дата:</b> {state['data'].get('date_display','')}\n"
                    f"<b>Время:</b> {state['data'].get('time','')}\n"
                    f"<b>Линия:</b> {state['data'].get('line','')}\n"
                    f"<b>Действие:</b> {'Запуск'}\n"
                    f"<b>Причина:</b> —\n"
                    f"<b>Пользователь:</b> {user_repr}"
                )
                cancel_flow(uid)
                send_message(chat_id, msg, reply_markup=main_menu_kb())
                return jsonify({"ok": True})
            else:
                # Остановка -> ask for reason using reasons sheet
                reasons = load_reasons()
                state["step"] = "reason"
                send_message(chat_id, "Причина остановки:", reply_markup=reasons_menu_kb(reasons))
                return jsonify({"ok": True})

        # Step: reason
        if step == "reason":
            reasons = load_reasons()
            if text == "Другое":
                state["step"] = "reason_custom"
                send_message(chat_id, "Введите причину остановки (текст):", reply_markup=cancel_kb())
                return jsonify({"ok": True})
            if text not in reasons:
                # treat as custom free text as well
                state["data"]["reason"] = text
            else:
                state["data"]["reason"] = text
            # finish and write
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = [
                state["data"].get("date_display", ""),
                state["data"].get("time", ""),
                state["data"].get("line", ""),
                state["data"].get("action", ""),
                state["data"].get("reason", ""),
                user_repr,
                ts
            ]
            append_to_startstop_sheet(row)
            msg = (
                "<b>Записано!</b>\n"
                f"<b>Дата:</b> {state['data'].get('date_display','')}\n"
                f"<b>Время:</b> {state['data'].get('time','')}\n"
                f"<b>Линия:</b> {state['data'].get('line','')}\n"
                f"<b>Действие:</b> {'Остановка'}\n"
                f"<b>Причина:</b> {state['data'].get('reason','')}\n"
                f"<b>Пользователь:</b> {user_repr}"
            )
            cancel_flow(uid)
            send_message(chat_id, msg, reply_markup=main_menu_kb())
            return jsonify({"ok": True})

        if step == "reason_custom":
            # free text reason
            state["data"]["reason"] = text
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = [
                state["data"].get("date_display", ""),
                state["data"].get("time", ""),
                state["data"].get("line", ""),
                state["data"].get("action", ""),
                state["data"].get("reason", ""),
                user_repr,
                ts
            ]
            append_to_startstop_sheet(row)
            msg = (
                "<b>Записано!</b>\n"
                f"<b>Дата:</b> {state['data'].get('date_display','')}\n"
                f"<b>Время:</b> {state['data'].get('time','')}\n"
                f"<b>Линия:</b> {state['data'].get('line','')}\n"
                f"<b>Действие:</b> {'Остановка'}\n"
                f"<b>Причина:</b> {state['data'].get('reason','')}\n"
                f"<b>Пользователь:</b> {user_repr}"
            )
            cancel_flow(uid)
            send_message(chat_id, msg, reply_markup=main_menu_kb())
            return jsonify({"ok": True})

        # default fallback
        cancel_flow(uid)
        send_message(chat_id, "Произошла ошибка. Начните заново.", reply_markup=main_menu_kb())
        return jsonify({"ok": True})

    except Exception:
        log.exception("Ошибка обработки update")
        return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
