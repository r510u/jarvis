import os
import json
import asyncio
import tempfile
from datetime import datetime, timedelta
import re

import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

# --- Конфиг ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- База данных ---
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            person TEXT,
            remind_at TIMESTAMP NOT NULL,
            done BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_reminder(chat_id, text, person, remind_at):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (chat_id, text, person, remind_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (chat_id, text, person, remind_at)
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row['id']

def mark_done(reminder_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE reminders SET done=TRUE WHERE id=%s", (reminder_id,))
    conn.commit()
    cur.close()
    conn.close()

def get_active_reminders(chat_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM reminders WHERE chat_id=%s AND done=FALSE AND remind_at > NOW() ORDER BY remind_at",
        (chat_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_due_reminders():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM reminders WHERE done=FALSE AND remind_at <= NOW()")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# --- GPT ---
SYSTEM_PROMPT = """Ты — Жарвис, умный помощник для менеджера по продажам.
Ты понимаешь команды на русском языке и отвечаешь ТОЛЬКО в формате JSON.

Распознавай намерения:
- "напомни", "напоминание", "не забудь" → action: "reminder"
- "создай встречу", "запланируй", "поставь встречу" → action: "meeting"
- "напиши сообщение", "отправь смс" → action: "message"
- остальное → action: "chat"

Для reminder возвращай:
{"action": "reminder", "text": "текст напоминания", "person": "имя или null", "datetime": "YYYY-MM-DD HH:MM или null", "delay_minutes": число или null}

Для meeting:
{"action": "meeting", "title": "название", "datetime": "YYYY-MM-DD HH:MM или null", "duration_minutes": число или 60, "participants": ["имена"]}

Для message:
{"action": "message", "to": "кому", "text": "текст"}

Для chat:
{"action": "chat", "reply": "твой ответ"}

Текущее время: {current_time}
Сегодня: {current_date}
ВАЖНО: возвращай ТОЛЬКО чистый JSON без markdown и лишних символов.
"""

def parse_ai_response(user_message: str) -> dict:
    now = datetime.now()
    system = SYSTEM_PROMPT.format(
        current_time=now.strftime("%H:%M"),
        current_date=now.strftime("%d.%m.%Y, %A")
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ],
        response_format={"type": "json_object"}
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise Exception(f'Не смог распарсить: {raw}')

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru"
        )
    return transcript.text

# --- Проверка напоминаний ---
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    due = get_due_reminders()
    for reminder in due:
        keyboard = [[
            InlineKeyboardButton("✅ Готово", callback_data=f"done_{reminder['id']}"),
            InlineKeyboardButton("⏰ Отложить 30 мин", callback_data=f"snooze_{reminder['id']}"),
        ]]
        text = f"🔔 *Напоминание*\n\n{reminder['text']}"
        if reminder.get('person'):
            text += f"\n👤 По: {reminder['person']}"
        try:
            await context.bot.send_message(
                chat_id=reminder['chat_id'],
                text=text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            mark_done(reminder['id'])
        except Exception as e:
            print(f"Ошибка отправки напоминания: {e}")

# --- Хендлеры ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 Привет! Я Жарвис — твой личный помощник.\n\n"
        f"Твой Chat ID: `{chat_id}`\n\n"
        f"Что умею:\n"
        f"• Напоминания — *«напомни позвонить Алексею завтра в 10»*\n"
        f"• Встречи — *«создай встречу с командой в пятницу в 15:00»*\n"
        f"• Голосовые сообщения 🎤\n"
        f"• Просто поговорить\n\n"
        f"Говори — я слушаю! 🎯",
        parse_mode='Markdown'
    )

async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("🤔 Думаю...")
    try:
        result = parse_ai_response(text)
        action = result.get("action")
        if action == "reminder":
            await handle_reminder(context, result, chat_id, thinking_msg)
        elif action == "meeting":
            await handle_meeting(result, thinking_msg)
        elif action == "message":
            await handle_message_draft(result, thinking_msg)
        else:
            await thinking_msg.edit_text(result.get("reply", "Понял тебя!"))
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Ошибка: {str(e)[:500]}")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_text(update, context, update.message.text)

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking_msg = await update.message.reply_text("🎤 Распознаю голос...")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            text = await transcribe_voice(tmp.name)
        await thinking_msg.edit_text(f"🎤 Распознал: _{text}_", parse_mode='Markdown')
        await process_text(update, context, text)
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Не смог распознать голос: {str(e)}")

async def handle_reminder(context, result, chat_id, thinking_msg):
    text = result.get("text", "Напоминание")
    person = result.get("person")
    dt_str = result.get("datetime")
    delay = result.get("delay_minutes")
    when = None
    when_text = ""
    if dt_str:
        try:
            when = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            when_text = when.strftime("%d.%m.%Y в %H:%M")
        except:
            pass
    if not when and delay:
        when = datetime.now() + timedelta(minutes=int(delay))
        when_text = f"через {int(delay)} мин"
    if not when:
        when = datetime.now() + timedelta(hours=1)
        when_text = "через 1 час"
    save_reminder(chat_id, text, person, when)
    msg = f"✅ *Напоминание создано!*\n\n📝 {text}"
    if person:
        msg += f"\n👤 По: {person}"
    msg += f"\n⏰ Когда: {when_text}"
    await thinking_msg.edit_text(msg, parse_mode='Markdown')

async def handle_meeting(result, thinking_msg):
    title = result.get("title", "Встреча")
    dt_str = result.get("datetime")
    duration = result.get("duration_minutes", 60)
    participants = result.get("participants", [])
    when_text = dt_str if dt_str else "время не указано"
    msg = f"📅 *Встреча создана!*\n\n📌 {title}\n⏰ {when_text}\n⌛ {duration} минут\n"
    if participants:
        msg += f"👥 Участники: {', '.join(participants)}\n"
    msg += "\n_Скоро добавлю интеграцию с Google Calendar!_"
    await thinking_msg.edit_text(msg, parse_mode='Markdown')

async def handle_message_draft(result, thinking_msg):
    to = result.get("to", "")
    text = result.get("text", "")
    msg = f"✉️ *Черновик для {to}:*\n\n_{text}_\n\n_(Скопируй и отправь сам)_"
    await thinking_msg.edit_text(msg, parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("done_"):
        reminder_id = int(data.replace("done_", ""))
        mark_done(reminder_id)
        await query.edit_message_text("✅ Отлично, задача выполнена!")
    elif data.startswith("snooze_"):
        reminder_id = int(data.replace("snooze_", ""))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM reminders WHERE id=%s", (reminder_id,))
        reminder = cur.fetchone()
        cur.close()
        conn.close()
        if reminder:
            new_time = datetime.now() + timedelta(minutes=30)
            save_reminder(reminder['chat_id'], reminder['text'], reminder.get('person'), new_time)
            mark_done(reminder_id)
            await query.edit_message_text("⏰ Отложено на 30 минут!")

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reminders = get_active_reminders(chat_id)
    if not reminders:
        await update.message.reply_text("📭 Нет активных напоминаний")
        return
    msg = "📋 *Активные напоминания:*\n\n"
    for r in reminders:
        when = r['remind_at'].strftime("%d.%m %H:%M")
        msg += f"• {when} — {r['text']}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.job_queue.run_repeating(check_reminders, interval=30, first=10)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    print("🚀 Жарвис запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
