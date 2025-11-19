# bot_webhook.py - FIFO queue + single worker + 10 min timeout
# Simplified integrated version due to size limits; extend as needed.

import os, json, logging, requests, threading, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort
import gspread

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH")

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_CREDS_PATH:
    raise RuntimeError("Missing env vars")

gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)
sh = gc.open_by_key(SPREADSHEET_ID)

STARTSTOP_SHEET_NAME = "Старт-Стоп"
HEADERS = ['Дата','Время','Номер линии','Действие','Причина','ЗНП','Метров брака','Пользователь','Время отправки','Статус']

def get_ws():
    try:
        ws=sh.worksheet(STARTSTOP_SHEET_NAME)
    except:
        ws=sh.add_worksheet(STARTSTOP_SHEET_NAME,1000,20)
    if ws.row_values(1)!=HEADERS:
        ws.clear()
        ws.insert_row(HEADERS,1)
    return ws

ws=get_ws()

def append_row(d):
    row=[d.get('date'),d.get('time'),d.get('line'),d.get('action'),
         d.get('reason',''),d.get('znp'),d.get('meters'),
         d.get('user'),d.get('ts'),d.get('status','')]
    ws.append_row(row,value_input_option='USER_ENTERED')

app=Flask(__name__)
TG=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def send(chat,text,markup=None):
    payload={"chat_id":chat,"text":text,"parse_mode":"HTML"}
    if markup:
        payload["reply_markup"]=json.dumps(markup,ensure_ascii=False)
    try:
        requests.post(TG,json=payload,timeout=10)
    except:
        log.exception("send fail")

# --- Global FIFO queue ---
queue=[]
queue_lock=threading.Lock()
queue_event=threading.Event()

# user states + timestamps
states={}
last_activity={}

TIMEOUT=600  #10 minutes

def timeout_checker():
    while True:
        time.sleep(30)
        now=time.time()
        for uid in list(states.keys()):
            if now - last_activity.get(uid,now) > TIMEOUT:
                chat=states[uid]["chat"]
                send(chat,"Диалог завершён из-за отсутствия активности (10 минут).")
                states.pop(uid,None)

def worker():
    while True:
        queue_event.wait()
        while True:
            with queue_lock:
                if not queue:
                    queue_event.clear()
                    break
                upd=queue.pop(0)
            process_update(upd)

threading.Thread(target=worker,daemon=True).start()
threading.Thread(target=timeout_checker,daemon=True).start()

# Keyboards
def kb(rows):
    return {"keyboard":[[{"text":t} for t in r] for r in rows],"resize_keyboard":True}

main_kb = kb([
    ["/start", "Старт/Стоп"],
    ["Брак"],
    ["Отменить последнюю запись"]
])
cancel_kb=kb([["Отмена"]])

def process_update(update):
    msg=update.get("message")
    if not msg:return
    text=(msg.get("text") or "").strip()
    chat=msg["chat"]["id"]
    uid=msg["from"]["id"]
    user_repr=f"{uid} (@{msg['from'].get('username','') or 'без_username'})"

    last_activity[uid]=time.time()

    # no flow
    if uid not in states:
        if text=="Старт/Стоп":
            states[uid]={"step":"line","data":{},"chat":chat}
            send(chat,"Номер линии (1‑15):",cancel_kb);return
        send(chat,"Выберите действие:",main_kb);return

    if text=="Отмена":
        states.pop(uid,None)
        send(chat,"Отменено.",main_kb)
        return

    st=states[uid]; step=st["step"]; data=st["data"]

    if step=="line":
        if not(text.isdigit() and 1<=int(text)<=15):
            send(chat,"Введите номер 1‑15:",cancel_kb);return
        data["line"]=text; st["step"]="date"
        today=datetime.now().strftime('%d.%m.%Y')
        yest=(datetime.now()-timedelta(days=1)).strftime('%d.%m.%Y')
        send(chat,"Дата:",kb([[today,yest],["Другая дата","Отмена"]]))
        return

    if step=="date":
        if text=="Другая дата":
            st["step"]="date_custom"; send(chat,"Введите дату дд.мм.гггг:",cancel_kb);return
        try:
            d,m,y=map(int,text.split('.'))
            datetime(y,m,d)
            data["date"]=text
            st["step"]="time"
            now=datetime.now()
            times=[(now-timedelta(minutes=m)).strftime('%H:%M') for m in (0,10,20,30)]
            send(chat,"Время:",kb([times[:2]+["Другое время"],times[2:]+["Отмена"]]))
            return
        except:
            send(chat,"Неверная дата:",cancel_kb);return

    if step=="date_custom":
        try:
            d,m,y=map(int,text.split('.')); datetime(y,m,d)
            data["date"]=text; st["step"]="time"
            now=datetime.now()
            times=[(now-timedelta(minutes=m)).strftime('%H:%M') for m in (0,10,20,30)]
            send(chat,"Время:",kb([times[:2]+["Другое время"],times[2:]+["Отмена"]]))
            return
        except:
            send(chat,"Неверный формат даты:",cancel_kb);return

    if step=="time":
        if text=="Другое время":
            st["step"]="time_custom"; send(chat,"Введите чч:мм:",cancel_kb);return
        try:
            h,m=map(int,text.split(':'))
            data["time"]=text; st["step"]="action"
            send(chat,"Действие:",kb([["Запуск","Остановка"],["Отмена"]]))
            return
        except:
            send(chat,"Неверное время:",cancel_kb);return

    if step=="time_custom":
        try:
            h,m=map(int,text.split(':'))
            data["time"]=text; st["step"]="action"
            send(chat,"Действие:",kb([["Запуск","Остановка"],["Отмена"]]))
            return
        except:
            send(chat,"Неверный формат времени:",cancel_kb);return

    if step=="action":
        if text not in("Запуск","Остановка"):
            send(chat,"Выберите действие:",kb([["Запуск","Остановка"],["Отмена"]]))
            return
        data["action"]="запуск" if text=="Запуск" else "остановка"
        if data["action"]=="запуск":
            st["step"]="znp"; send(chat,"Номер ЗНП (4 цифры):",cancel_kb);return
        else:
            st["step"]="reason"; send(chat,"Причина остановки:",kb([["Другое"],["Отмена"]]))
            return

    if step=="reason":
        if text=="Другое":
            st["step"]="reason_custom"; send(chat,"Введите причину:",cancel_kb);return
        data["reason"]=text
        st["step"]='znp'; send(chat,"Номер ЗНП:",cancel_kb);return

    if step=="reason_custom":
        data["reason"]=text
        st["step"]="znp"; send(chat,"Номер ЗНП:",cancel_kb);return

    if step=="znp":
        if not(text.isdigit() and len(text)==4):
            send(chat,"Введите 4‑значный ЗНП:",cancel_kb);return
        data["znp"]=text
        st["step"]="meters"; send(chat,"Метров брака:",cancel_kb);return

    if step=="meters":
        if not text.isdigit():
            send(chat,"Введите число:",cancel_kb);return
        data["meters"]=text

        d=data; ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        append_row({
            "date":d["date"],"time":d["time"],"line":d["line"],
            "action":d["action"],"reason":d.get("reason",""),
            "znp":d["znp"],"meters":d["meters"],
            "user":user_repr,"ts":ts,"status":""
        })

        send(chat,
             f"<b>Записано!</b>\n"
             f"Дата: {d['date']}\nВремя: {d['time']}\nЛиния: {d['line']}\n"
             f"Действие: {'Запуск' if d['action']=='запуск' else 'Остановка'}\n"
             f"Причина: {d.get('reason','—')}\n"
             f"ЗНП: {d['znp']}\nМетров брака: {d['meters']}",
             kb([["Старт/Стоп"],["Брак"],["Отменить последнюю запись"]])
        )
        states.pop(uid,None)
        return

@app.route("/health")
def health():
    return {"ok":True}

@app.route(f"/webhook/{TELEGRAM_TOKEN}",methods=["POST"])
def webhook():
    upd=request.get_json()
    if not upd:return {"ok":True}
    with queue_lock:
        queue.append(upd)
        queue_event.set()
    return {"ok":True}

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
