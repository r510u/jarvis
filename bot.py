import os
import json
import tempfile
import re
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

# --- БД ---
def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            person TEXT,
            remind_at TIMESTAMP NOT NULL,
            done BOOLEAN DEFAULT FALSE
        )
    """)
    conn.commit(); cur.close(); conn.close()

def save_reminder(chat_id, text, person, remind_at):
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO reminders (chat_id,text,person,remind_at) VALUES (%s,%s,%s,%s) RETURNING id",
                (chat_id, text, person, remind_at))
    rid = cur.fetchone()['id']
    conn.commit(); cur.close(); conn.close()
    return rid

def mark_done(rid):
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE reminders SET done=TRUE WHERE id=%s", (rid,))
    conn.commit(); cur.close(); conn.close()

def get_due():
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE done=FALSE AND remind_at <= NOW()")
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

def get_active(chat_id):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE chat_id=%s AND done=FALSE AND remind_at > NOW() ORDER BY remind_at", (chat_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

# --- GPT ---
SYSTEM = """Ты — Жарвис, помощник менеджера по продажам. Отвечай ТОЛЬКО валидным JSON.

Определи намерение и верни JSON:

Если напоминание ("напомни", "не забудь"):
{"action":"reminder","text":"текст","person":"имя или null","datetime":"YYYY-MM-DD HH:MM или null","delay_minutes":число или null}

Если встреча ("создай встречу", "запланируй"):
{"action":"meeting","title":"название","datetime":"YYYY-MM-DD HH:MM или null","duration_minutes":60,"participants":[]}

Если черновик сообщения ("напиши", "отправь сообщение"):
{"action":"message","to":"кому","text":"текст"}

Иначе:
{"action":"chat","reply":"ответ"}

Сейчас: {time}, {date}"""

def ask_gpt(text):
    now = datetime.now()
    system = SYSTEM.format(time=now.strftime("%H:%M"), date=now.strftime("%d.%m.%Y %A"))
    print(f"Вызываю GPT с текстом: {text}")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system},{"role":"user","content":text}],
            response_format={"type":"json_object"}
        )
        raw = resp.choices[0].message.content.strip()
        print(f"GPT raw: {raw}")
        return json.loads(raw)
    except Exception as e:
        print(f"GPT ошибка: {e}")
        raise

async def transcribe(path):
    with open(path, "rb") as f:
        t = client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
    return t.text

# --- Проверка напоминаний ---
async def tick(context: ContextTypes.DEFAULT_TYPE):
    for r in get_due():
        kb = [[
            InlineKeyboardButton("✅ Готово", callback_data=f"done_{r['id']}"),
            InlineKeyboardButton("⏰ +30 мин", callback_data=f"snooze_{r['id']}"),
        ]]
        msg = f"🔔 *Напоминание*\n\n{r['text']}"
        if r.get('person'): msg += f"\n👤 {r['person']}"
        try:
            await context.bot.send_message(r['chat_id'], msg, parse_mode='Markdown',
                                           reply_markup=InlineKeyboardMarkup(kb))
            mark_done(r['id'])
        except Exception as e:
            print(f"Ошибка отправки: {e}")

# --- Хендлеры ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Привет! Я Жарвис.\n\n"
        f"Твой Chat ID: `{update.effective_chat.id}`\n\n"
        f"Умею:\n• Напоминания — *«напомни позвонить Алексею завтра в 10»*\n"
        f"• Встречи — *«создай встречу с командой в пятницу в 15:00»*\n"
        f"• Голосовые сообщения 🎤\n\n"
        f"Говори!", parse_mode='Markdown')

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rs = get_active(update.effective_chat.id)
    if not rs:
        await update.message.reply_text("📭 Нет активных напоминаний"); return
    msg = "📋 *Напоминания:*\n\n"
    for r in rs:
        msg += f"• {r['remind_at'].strftime('%d.%m %H:%M')} — {r['text']}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    m = await update.message.reply_text("🤔 Думаю...")
    try:
        r = ask_gpt(text)
        action = r.get("action", "chat")

        if action == "reminder":
            text_r = r.get("text", "Напоминание")
            person = r.get("person")
            dt_str = r.get("datetime")
            delay = r.get("delay_minutes")
            when = None; when_str = ""

            if dt_str:
                try:
                    when = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    when_str = when.strftime("%d.%m.%Y в %H:%M")
                except: pass

            if not when and delay:
                when = datetime.now() + timedelta(minutes=int(delay))
                when_str = f"через {int(delay)} мин"

            if not when:
                when = datetime.now() + timedelta(hours=1)
                when_str = "через 1 час"

            save_reminder(chat_id, text_r, person, when)
            msg = f"✅ *Создано!*\n\n📝 {text_r}"
            if person: msg += f"\n👤 {person}"
            msg += f"\n⏰ {when_str}"
            await m.edit_text(msg, parse_mode='Markdown')

        elif action == "meeting":
            title = r.get("title", "Встреча")
            dt = r.get("datetime", "не указано")
            dur = r.get("duration_minutes", 60)
            parts = r.get("participants", [])
            msg = f"📅 *Встреча:* {title}\n⏰ {dt}\n⌛ {dur} мин"
            if parts: msg += f"\n👥 {', '.join(parts)}"
            await m.edit_text(msg, parse_mode='Markdown')

        elif action == "message":
            to = r.get("to", "")
            txt = r.get("text", "")
            await m.edit_text(f"✉️ *Для {to}:*\n\n_{txt}_", parse_mode='Markdown')

        else:
            await m.edit_text(r.get("reply", "Понял!"))

    except Exception as e:
        print(f"Ошибка: {e}")
        await m.edit_text(f"❌ Ошибка: {str(e)[:300]}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process(update, context, update.message.text)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("🎤 Распознаю...")
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await f.download_to_drive(tmp.name)
            text = await transcribe(tmp.name)
        await m.edit_text(f"🎤 _{text}_", parse_mode='Markdown')
        await process(update, context, text)
    except Exception as e:
        await m.edit_text(f"❌ Голос: {str(e)[:200]}")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data.startswith("done_"):
        mark_done(int(q.data.replace("done_", "")))
        await q.edit_message_text("✅ Готово!")
    elif q.data.startswith("snooze_"):
        rid = int(q.data.replace("snooze_", ""))
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT * FROM reminders WHERE id=%s", (rid,))
        r = cur.fetchone(); cur.close(); conn.close()
        if r:
            save_reminder(r['chat_id'], r['text'], r.get('person'), datetime.now()+timedelta(minutes=30))
            mark_done(rid)
        await q.edit_message_text("⏰ Отложено на 30 мин!")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.job_queue.run_repeating(tick, interval=30, first=10)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("🚀 Жарвис запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
