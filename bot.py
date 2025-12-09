import os
import asyncio
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# Загружаем переменные окружения
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# Храним треды OpenAI для пользователей
user_threads = {}

# Главная клавиатура
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

    # Создаем тред OpenAI
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id

    welcome_text = (
        "Привет! 👋\n"
        "Я — официальный ассистент Maison de Café.\n"
        "Готов ответить на любые вопросы о стоимости, оборудовании, окупаемости и запуске кофейни.\n\n"
        "Выбери вопрос на клавиатуре или напиши свой:"
    )

    await update.message.reply_text(welcome_text, reply_markup=MAIN_KEYBOARD)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    # Если нет треда — создаем
    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id

    thread_id = user_threads[user_id]

    # Отправляем сообщение пользователя в OpenAI
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text
    )

    # Запускаем процесс ассистента
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID
    )

    # Ждем завершения
    while True:
        status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if status.status == "completed":
            break
        await asyncio.sleep(1)

    # Получаем ответ ассистента
    messages = client.beta.threads.messages.list(thread_id=thread_id)
    ai_reply = messages.data[0].content[0].text.value

    await update.message.reply_text(ai_reply, reply_markup=MAIN_KEYBOARD)


def main():
    print("🚀 Бот запускается...")
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
