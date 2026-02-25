import os
import json
import asyncio
from datetime import datetime, timedelta
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

# --- Конфиг ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
YOUR_CHAT_ID = os.environ.get("YOUR_CHAT_ID")  # твой Telegram chat_id

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Хранилище напоминаний в памяти (потом можно заменить на БД)
reminders = []

SYSTEM_PROMPT = """Ты — Жарвис, умный помощник для менеджера по продажам.
Ты понимаешь команды на русском языке и отвечаешь ТОЛЬКО в формате JSON.

Распознавай намерения:
- "напомни", "напоминание", "не забудь" → action: "reminder"
- "создай встречу", "запланируй", "поставь встречу" → action: "meeting"  
- "напиши сообщение", "отправь смс" → action: "message"
- остальное → action: "chat"

Для reminder возвращай:
{
  "action": "reminder",
  "text": "текст напоминания",
  "person": "имя человека или null",
  "datetime": "YYYY-MM-DD HH:MM или null",
  "delay_minutes": число минут от сейчас или null
}

Для meeting:
{
  "action": "meeting",
  "title": "название встречи",
  "datetime": "YYYY-MM-DD HH:MM или null",
  "duration_minutes": число или 60,
  "participants": ["имена"] 
}

Для message:
{
  "action": "message",
  "to": "кому",
  "text": "текст сообщения"
}

Для chat:
{
  "action": "chat",
  "reply": "твой ответ"
}

Текущее время: {current_time}
Сегодня: {current_date}
"""


def parse_ai_response(user_message: str) -> dict:
    """Отправляем сообщение в GPT и получаем структурированный ответ"""
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
    
    return json.loads(response.choices[0].message.content)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет напоминание пользователю"""
    job = context.job
    data = job.data
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Готово", callback_data=f"done_{job.name}"),
            InlineKeyboardButton("⏰ Отложить на 30 мин", callback_data=f"snooze_{job.name}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"🔔 *Напоминание*\n\n{data['text']}"
    if data.get('person'):
        text += f"\n👤 По: {data['person']}"
    
    await context.bot.send_message(
        chat_id=data['chat_id'],
        text=text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 Привет! Я Жарвис — твой личный помощник.\n\n"
        f"Твой Chat ID: `{chat_id}`\n\n"
        f"Что умею:\n"
        f"• Напоминания — *«напомни позвонить Алексею завтра в 10»*\n"
        f"• Встречи — *«создай встречу с командой в пятницу в 15:00»*\n"
        f"• Просто поговорить\n\n"
        f"Говори — я слушаю! 🎯",
        parse_mode='Markdown'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id
    
    # Показываем что думаем
    thinking_msg = await update.message.reply_text("🤔 Думаю...")
    
    try:
        result = parse_ai_response(user_message)
        action = result.get("action")
        
        if action == "reminder":
            await handle_reminder(update, context, result, chat_id, thinking_msg)
        
        elif action == "meeting":
            await handle_meeting(update, context, result, thinking_msg)
        
        elif action == "message":
            await handle_message_draft(update, result, thinking_msg)
        
        else:
            # Обычный чат
            reply = result.get("reply", "Понял тебя!")
            await thinking_msg.edit_text(reply)
    
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Что-то пошло не так: {str(e)}")


async def handle_reminder(update, context, result, chat_id, thinking_msg):
    text = result.get("text", "Напоминание")
    person = result.get("person")
    dt_str = result.get("datetime")
    delay = result.get("delay_minutes")
    
    # Определяем когда
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
        when_text = f"через {delay} мин"
    
    if not when:
        # По умолчанию через 1 час
        when = datetime.now() + timedelta(hours=1)
        when_text = "через 1 час"
    
    # Планируем задачу
    job_name = f"reminder_{chat_id}_{len(reminders)}"
    job_data = {"text": text, "person": person, "chat_id": chat_id}
    
    context.job_queue.run_once(
        send_reminder,
        when=when,
        data=job_data,
        name=job_name,
        chat_id=chat_id
    )
    reminders.append(job_name)
    
    msg = f"✅ *Напоминание создано!*\n\n📝 {text}"
    if person:
        msg += f"\n👤 По: {person}"
    msg += f"\n⏰ Когда: {when_text}"
    
    await thinking_msg.edit_text(msg, parse_mode='Markdown')


async def handle_meeting(update, context, result, thinking_msg):
    title = result.get("title", "Встреча")
    dt_str = result.get("datetime")
    duration = result.get("duration_minutes", 60)
    participants = result.get("participants", [])
    
    when_text = dt_str if dt_str else "время не указано"
    
    msg = f"📅 *Встреча создана!*\n\n"
    msg += f"📌 {title}\n"
    msg += f"⏰ {when_text}\n"
    msg += f"⌛ {duration} минут\n"
    if participants:
        msg += f"👥 Участники: {', '.join(participants)}\n"
    
    msg += "\n_Скоро добавлю интеграцию с Google Calendar!_"
    
    await thinking_msg.edit_text(msg, parse_mode='Markdown')


async def handle_message_draft(update, result, thinking_msg):
    to = result.get("to", "")
    text = result.get("text", "")
    
    msg = f"✉️ *Черновик сообщения для {to}:*\n\n"
    msg += f"_{text}_\n\n"
    msg += "_(Скопируй и отправь сам — пока работаю над автоотправкой)_"
    
    await thinking_msg.edit_text(msg, parse_mode='Markdown')


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("done_"):
        await query.edit_message_text("✅ Отлично, задача выполнена!")
    
    elif data.startswith("snooze_"):
        job_name = data.replace("snooze_", "snooze2_")
        chat_id = query.message.chat_id
        
        # Откладываем на 30 минут
        original_text = query.message.text
        context.job_queue.run_once(
            send_reminder,
            when=timedelta(minutes=30),
            data={"text": original_text, "person": None, "chat_id": chat_id},
            name=job_name,
            chat_id=chat_id
        )
        await query.edit_message_text("⏰ Отложено на 30 минут!")


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("📭 Нет активных напоминаний")
        return
    
    msg = "📋 *Активные напоминания:*\n\n"
    for i, job in enumerate(jobs, 1):
        if job.next_t:
            when = job.next_t.strftime("%d.%m %H:%M")
            msg += f"{i}. ⏰ {when}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 Жарвис запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
