import os
import asyncio
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

user_threads = {}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Что такое Maison de Café?", "Сколько стоит открыть кофейню?"],
        ["Окупаемость и прибыль", "Помощь с выбором локации"],
        ["Условия франшизы", "Контакты / связь с владельцем"],
    ],
    resize_keyboard=True
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id

    text = (
        "Привет! 👋\n"
        "Я — официальный ассистент Maison de Café. Готов помочь тебе узнать всё о кофейне, стоимости, окупаемости, оборудовании и франшизе.\n\n"
        "Выбери вопрос на клавиатуре ниже или напиши свой:"
    )

    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id

    thread_id = user_threads[user_id]

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID
    )

    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )
        if run_status.status == "completed":
            break
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    ai_reply = messages.data[0].content[0].text.value

    await update.message.reply_text(ai_reply, reply_markup=MAIN_KEYBOARD)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен. Ожидаю сообщения…")
    app.run_polling()

if __name__ == "__main__":
    main()
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
print(
    "DEBUG TELEGRAM TOKEN:",
    "empty=" + str(TELEGRAM_TOKEN in [None, ""]),
    "len=" + str(len(TELEGRAM_TOKEN or "")),
    "has_colon=" + str(":" in (TELEGRAM_TOKEN or "")),
)
