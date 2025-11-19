# bot_webhook.py
# updated: integrated full Start/Stop flow (ZNP, meters), kept original structure and features
import os
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort, jsonify
import gspread
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

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
# If necessary, you can change this sheet name to whatever you actually have in Google Sheets.
STARTSTOP_SHEET_NAME = os.environ.get('STARTSTOP_SHEET_NAME', 'Старт-Стоп')

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
                path = None
                for fname in os.listdir(SECRETS_DIR):
                    if fname.lower().endswith(".json"):
                        path = os.path.join(SECRETS_DIR, fname)
                        log.info(f"Найден JSON файл секретов: {path}, попробую использовать его")
                        gc = gspread.service_account(filename=path)
                        break
                if not path:
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
# Config for StartStop sheet (headers include ZNP and meters)
# ----------------------------
HEADERS_STARTSTOP = [
    'Дата', 'Время', 'Номер линии', 'Действие', 'Причина',
    'ЗНП', 'Метров брака', 'Пользователь', 'Время отправки', 'Статус'
]

# ----------------------------
# Worksheet helpers
# ----------------------------
def get_or_create_ws_by_name(workbook, name):
    try:
        ws = workbook.worksheet(name)
        log.info(f"Использую лист '{name}'")
        return ws
    except Exception:
        # create
        try:
            ws = workbook.add_worksheet(title=name, rows=1000, cols=max(10, len(HEADERS_STARTSTOP)))
            log.info(f"Создан лист '{name}'")
            return ws
        except Exception:
            log.exception(f"Не удалось создать лист '{name}', использую первый лист")
            return workbook.sheet1

def ensure_headers(ws, headers):
    try:
        current = ws.row_values(1)
    except Exception:
        current = []
    current_norm = [c.strip() if isinstance(c, str) else c for c in current]
    headers_norm = [h for h in headers]

    if not current_norm or all((not str(c).strip()) for c in current_norm):
        try:
            ws.insert_row(headers_norm, index=1)
            log.info("Вставлены заголовки в листе")
        except Exception:
            try:
                ws.update('A1', [headers_norm])
                log.info("Заголовки записаны через update")
            except Exception:
                log.exception("Не удалось записать заголовки")
        return

    if current_norm == headers_norm:
        return

    try:
        all_values = ws.get_all_values()
    except Exception:
        all_values = []
    data_rows_count = max(0, len(all_values) - 1)
    if data_rows_count == 0:
        try:
            ws.delete_rows(1)
            ws.insert_row(headers_norm, index=1)
            log.info("Заголовки заменены (лист не содержал данных)")
        except Exception:
            try:
                ws.update('A1', [headers_norm])
                log.info("Заголовки заменены через update")
            except Exception:
                log.exception("Не удалось заменить заголовки")
        return
    else:
        log.warning("Заголовки листа отличаются от ожидаемых, но лист содержит данные — не изменяю заголовки автоматически.")
        return

startstop_ws = get_or_create_ws_by_name(sh, STARTSTOP_SHEET_NAME)
ensure_headers(startstop_ws, HEADERS_STARTSTOP)

def append_to_startstop_sheet_by_headers(data_dict):
    try:
        ensure_headers(startstop_ws, HEADERS_STARTSTOP)
        row = []
        for h in HEADERS_STARTSTOP:
            if h == 'Дата':
                row.append(data_dict.get('date_display', ''))
            elif h == 'Время':
                row.append(data_dict.get('time', ''))
            elif h == 'Номер линии':
                row.append(data_dict.get('line', ''))
            elif h == 'Действие':
                action = data_dict.get('action', '')
                row.append('Запуск' if action == 'запуск' else 'Остановка' if action == 'остановка' else action)
            elif h == 'Причина':
                row.append(data_dict.get('reason', ''))
            elif h == 'ЗНП':
                row.append(data_dict.get('znp', ''))
            elif h == 'Метров брака':
                row.append(data_dict.get('meters', ''))
            elif h == 'Пользователь':
                row.append(data_dict.get('user_repr', ''))
            elif h == 'Время отправки':
                row.append(data_dict.get('timestamp', ''))
            elif h == 'Статус':
                row.append(data_dict.get('status', ''))
            else:
                row.append(data_dict.get(h, ''))
        startstop_ws.append_row(row, value_input_option='USER_ENTERED')
        log.info("Запись добавлена в 'Старт-Стоп' (по заголовкам)")
    except Exception:
        log.exception("Ошибка при добавлении записи в лист 'Старт-Стоп'")

# ----------------------------
# Reasons helper
# ----------------------------
def load_reasons():
    try:
        for name in ['Причина остановки', 'Причины', 'Reasons']:
            try:
                ws = sh.worksheet(name)
                vals = ws.col_values(1)
                res = [v.strip() for v in vals if v and v.strip()]
                # если первая строка — заголовок, опускаем
                if res and res[0].lower().startswith('прич'):
                    res = res[1:]
                if not res:
                    return ['Другое']
                if 'Другое' not in res:
                    res.append('Другое')
                return res
            except Exception:
                continue
        return ['Другое']
    except Exception:
        log.exception("Ошибка при загрузке причин")
        return ['Другое']

# ----------------------------
# Keyboards (one_time=True for choice menus)
# ----------------------------
def keyboard_from_rows(rows, one_time=False, resize=True):
    kb = {
        "keyboard": [[{"text": text} for text in row] for row in rows],
        "one_time_keyboard": one_time,
        "resize_keyboard": resize,
    }
    return kb

def main_menu_kb():
    # кнопки по содержимому (размер подтягивается Telegram сам)
    return keyboard_from_rows([["Старт/Стоп"], ["Брак"], ["Отменить последнюю запись"]])

def cancel_kb():
    return keyboard_from_rows([["Отмена"]])

def date_menu_kb():
    today = datetime.now().strftime('%d.%m.%Y')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%d.%m.%Y')
    return keyboard_from_rows([[today, yesterday], ["Другая дата", "Отмена"]], one_time=True)

def time_menu_kb():
    now = datetime.now()
    times = []
    for mins in [0, 10, 20, 30]:
        t = (now - timedelta(minutes=mins)).strftime('%H:%M')
        times.append(t)
    # arrange into two rows and 'Другое время' + 'Отмена'
    row1 = times[:2] + ['Другое время']
    row2 = times[2:] + ['Отмена']
    return keyboard_from_rows([row1, row2], one_time=True)

def action_menu_kb():
    return keyboard_from_rows([["Запуск", "Остановка"], ["Отмена"]], one_time=True)

def reasons_menu_kb(reasons_list):
    rows = []
    r = list(reasons_list)
    if 'Другое' not in r:
        r.append('Другое')
    for i in range(0, len(r), 2):
        rows.append(r[i:i+2])
    rows.append(["Отмена"])
    return keyboard_from_rows(rows, one_time=True)

# ----------------------------
# Conversation state
# ----------------------------
user_states = {}
user_locks = {}             # uid -> threading.Lock
user_locks_lock = threading.Lock()

def get_user_lock(uid):
    with user_locks_lock:
        l = user_locks.get(uid)
        if not l:
            l = threading.Lock()
            user_locks[uid] = l
        return l

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
        datetime(year=y, month=m, day=d)
        # Return ISO for internal use + display same as input
        return f"{y:04d}-{m:02d}-{d:02d}", s
    except Exception:
        return None, None

def validate_time_input(s):
    import re
    if re.match(r'^\d{2}:\d{2}$', s):
        hh, mm = map(int, s.split(':'))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return True
    return False

def validate_znp(s):
    return s.isdigit() and len(s) == 4

def validate_meters(s):
    # allow integer >= 0
    return s.isdigit()

# ----------------------------
# Telegram helpers
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
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("Ошибка отправки сообщения в Telegram")
        return None

# ----------------------------
# Deduplication and async executor
# ----------------------------
processed_updates_lock = threading.Lock()
processed_update_ids = deque(maxlen=2000)
processed_update_ids_set = set()

executor = ThreadPoolExecutor(max_workers=4)

def is_duplicate_update(update_id):
    with processed_updates_lock:
        if update_id in processed_update_ids_set:
            return True
        processed_update_ids.append(update_id)
        processed_update_ids_set.add(update_id)
        # keep set in sync with deque size
        if len(processed_update_ids) == processed_update_ids.maxlen:
            # rebuild set to avoid stale entries (safe)
            processed_update_ids_set.clear()
            processed_update_ids_set.update(processed_update_ids)
        return False

# ----------------------------
# Core update processing (moved from webhook handler)
# ----------------------------
def process_update(update):
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = message.get("text", "").strip()
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        uid = from_user.get("id")
        username = from_user.get("username") or ""
        user_repr = f"{uid} (@{username or 'без_username'})"

        ulock = get_user_lock(uid)
        acquired = ulock.acquire(timeout=5)
        if not acquired:
            log.warning(f"Не удалось захватить lock для пользователя {uid}, пропускаю update")
            return
        try:
            # If no active conversation -> react to main menu buttons or commands
            if uid not in user_states:
                if text == "Старт/Стоп":
                    start_startstop_flow(uid, from_user, chat_id)
                    send_message(chat_id, "Номер линии (1-15):", reply_markup=cancel_kb())
                    return
                elif text == "Брак":
                    send_message(chat_id, "Раздел «Брак» пока не реализован.", reply_markup=main_menu_kb())
                    return
                elif text == "Отменить последнюю запись":
                    send_message(chat_id, "Функция отмены пока не реализована.", reply_markup=main_menu_kb())
                    return
                else:
                    send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
                    return

            state = user_states.get(uid)
            if not state or state.get("flow") != "startstop":
                cancel_flow(uid)
                send_message(chat_id, "Произошла ошибка состояния. Начните заново.", reply_markup=main_menu_kb())
                return

            step = state.get("step")
            if text == "Отмена":
                cancel_flow(uid)
                send_message(chat_id, "Отменено.", reply_markup=main_menu_kb())
                return

            # ---------- FLOW STEPS ----------
            if step == "line":
                if not text.isdigit() or not (1 <= int(text) <= 15):
                    send_message(chat_id, "Введите номер линии от 1 до 15 (целое число):", reply_markup=cancel_kb())
                    return
                state["data"]["line"] = text
                state["step"] = "date"
                send_message(chat_id, "Дата (дд.мм.гггг):", reply_markup=date_menu_kb())
                return

            if step == "date":
                if text == "Другая дата":
                    send_message(chat_id, "Введите дату в формате дд.мм.гггг:", reply_markup=cancel_kb())
                    state["step"] = "date_custom"
                    return
                parsed_iso, display = parse_date_input(text)
                if not parsed_iso:
                    send_message(chat_id, "Неверный формат даты. Введите в формате дд.мм.гггг или выберите кнопку:", reply_markup=date_menu_kb())
                    return
                state["data"]["date_iso"] = parsed_iso
                state["data"]["date_display"] = display
                state["step"] = "time"
                send_message(chat_id, "Время (чч:мм):", reply_markup=time_menu_kb())
                return

            if step == "date_custom":
                parsed_iso, display = parse_date_input(text)
                if not parsed_iso:
                    send_message(chat_id, "Неверный формат. Введите дату в формате дд.мм.гггг:", reply_markup=cancel_kb())
                    return
                state["data"]["date_iso"] = parsed_iso
                state["data"]["date_display"] = display
                state["step"] = "time"
                send_message(chat_id, "Время (чч:мм):", reply_markup=time_menu_kb())
                return

            if step == "time":
                if text == "Другое время":
                    send_message(chat_id, "Введите время в формате чч:мм:", reply_markup=cancel_kb())
                    state["step"] = "time_custom"
                    return
                if not validate_time_input(text):
                    send_message(chat_id, "Неверный формат времени. Введите чч:мм или выберите кнопку:", reply_markup=time_menu_kb())
                    return
                state["data"]["time"] = text
                state["step"] = "action"
                send_message(chat_id, "Действие:", reply_markup=action_menu_kb())
                return

            if step == "time_custom":
                if not validate_time_input(text):
                    send_message(chat_id, "Неверный формат времени. Введите чч:мм:", reply_markup=cancel_kb())
                    return
                state["data"]["time"] = text
                state["step"] = "action"
                send_message(chat_id, "Действие:", reply_markup=action_menu_kb())
                return

            if step == "action":
                if text not in ["Запуск", "Остановка"]:
                    send_message(chat_id, "Выберите действие: Запуск или Остановка", reply_markup=action_menu_kb())
                    return
                action = "запуск" if text == "Запуск" else "остановка"
                state["data"]["action"] = action
                # if start -> request ZNP -> meters -> save
                if action == "запуск":
                    state["step"] = "znp"
                    send_message(chat_id, "Номер ЗНП (4 цифры):", reply_markup=cancel_kb())
                    return
                else:
                    # остановка -> сначала причина, потом ZNP и meters
                    reasons = load_reasons()
                    state["step"] = "reason"
                    send_message(chat_id, "Причина остановки:", reply_markup=reasons_menu_kb(reasons))
                    return

            # ---- reason for stop ----
            if step == "reason":
                reasons = load_reasons()
                if text == "Другое":
                    state["step"] = "reason_custom"
                    send_message(chat_id, "Введите причину остановки (текст):", reply_markup=cancel_kb())
                    return
                # если выбрана причина из списка или введена произвольно
                state["data"]["reason"] = text
                # теперь запрос ЗНП
                state["step"] = "znp"
                send_message(chat_id, "Номер ЗНП (4 цифры):", reply_markup=cancel_kb())
                return

            if step == "reason_custom":
                state["data"]["reason"] = text
                state["step"] = "znp"
                send_message(chat_id, "Номер ЗНП (4 цифры):", reply_markup=cancel_kb())
                return

            # ---- znp (applies to both старт и остановка) ----
            if step == "znp":
                if not validate_znp(text):
                    send_message(chat_id, "Неверный формат ЗНП. Введите ровно 4 цифры:", reply_markup=cancel_kb())
                    return
                state["data"]["znp"] = text
                state["step"] = "meters"
                send_message(chat_id, "Количество метров брака (целое число):", reply_markup=cancel_kb())
                return

            # ---- meters ----
            if step == "meters":
                if not validate_meters(text):
                    send_message(chat_id, "Введите количество метров брака числом (целое):", reply_markup=cancel_kb())
                    return
                state["data"]["meters"] = text

                # теперь формируем dict и сохраняем в таблицу
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                data_dict = {
                    "date_display": state["data"].get("date_display", ""),
                    "time": state["data"].get("time", ""),
                    "line": state["data"].get("line", ""),
                    "action": state["data"].get("action", ""),
                    "reason": state["data"].get("reason", ""),
                    "znp": state["data"].get("znp", ""),
                    "meters": state["data"].get("meters", ""),
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
                send_message(chat_id, msg, reply_markup=main_menu_kb())
                return

            # fallback
            cancel_flow(uid)
            send_message(chat_id, "Произошла ошибка. Начните заново.", reply_markup=main_menu_kb())
        finally:
            ulock.release()
    except Exception:
        log.exception("Ошибка в process_update")

# ----------------------------
# Webhook endpoint: quick 200, then background processing
# ----------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    # Minimal fast validation and dedupe, then enqueue processing
    if token != TELEGRAM_TOKEN:
        log.warning("Получён вебхук с неверным токеном в пути")
        abort(403)
    if request.headers.get("content-type") != "application/json":
        abort(400)
    update = request.get_json()
    if not update:
        return jsonify({"ok": True})

    update_id = update.get("update_id")
    if update_id is not None and is_duplicate_update(update_id):
        log.info(f"Дубликат update_id {update_id} — игнорирую")
        return jsonify({"ok": True})

    # enqueue processing and return 200 immediately
    try:
        executor.submit(process_update, update)
    except Exception:
        log.exception("Не удалось поставить задачу в исполнителе, попробую синхронно")
        # fallback to sync processing
        process_update(update)

    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
