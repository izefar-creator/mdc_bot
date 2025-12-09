import os
import asyncio
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ====== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID не задан в переменных окружения")

client = OpenAI(api_key=OPENAI_API_KEY)

# У каждого пользователя свой thread в ассистенте
user_threads: dict[str, str] = {}

# ====== КЛАВИАТУРА БОТА ======
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Что такое Maison de Café?", "Сколько стоит открыть кофейню?"],
        ["Окупаемость и прибыль", "Помощь с выбором локации"],
        ["Условия франшизы", "Контакты / связь с владельцем"],
    ],
    resize_keyboard=True
)

# ====== ХЭНДЛЕР КОМАНДЫ /start ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # создаём новый thread для пользователя
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id

    welcome_text = (
        "Привет! 👋\n"
        "Я — официальный ассистент Maison de Café.\n\n"
        "Отвечаю на вопросы о:\n"
        "• запуске кофейни самообслуживания\n"
        "• стоимости комплекта и оборудования\n"
        "• окупаемости и прибыли\n"
        "• франшизе и поддержке от Maison de Café\n\n"
        "Выбери вопрос ниже или напиши свой:"
    )

    await update.message.reply_text(welcome_text, reply_markup=MAIN_KEYBOARD)

# ====== ОБРАБОТКА ЛЮБОГО ТЕКСТОВОГО СООБЩЕНИЯ ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    # если у этого пользователя ещё нет thread — создаём
    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id

    thread_id = user_threads[user_id]

    # отправляем сообщение пользователя ассистенту
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text,
    )

    # запускаем run ассистента
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
    )

    # ждём, пока ассистент закончит думать
    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id,
        )
        if run_status.status == "completed":
            break
        elif run_status.status in ["failed", "cancelled", "expired"]:
            await update.message.reply_text("⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз.")
            return
        await asyncio.sleep(1)

    # забираем последний ответ ассистента
    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        await update.message.reply_text("⚠️ Не удалось получить ответ. Попробуйте ещё раз.")
        return

    # последний (самый свежий) ответ
    ai_reply = messages.data[0].content[0].text.value

    await update.message.reply_text(ai_reply, reply_markup=MAIN_KEYBOARD)

# ====== ТОЧКА ВХОДА ======
def main():
    print("🚀 Бот запускается...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # /start
    application.add_handler(CommandHandler("start", start))
    # любой текст
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
