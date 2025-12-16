import os
import re
import json
import time
import asyncio
import tempfile
import smtplib
from email.mime.text import MIMEText
from typing import Dict, Optional, Any

from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI


# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

# Owner notifications
OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")  # numeric Telegram user id as string, e.g. "250910730"

LEAD_EMAIL_TO = os.getenv("LEAD_EMAIL_TO", "maisondecafe.coffee@gmail.com")

# Optional SMTP (for sending lead emails)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or LEAD_EMAIL_TO)

# Contacts to show to customers
CONTACT_EMAIL_PUBLIC = os.getenv("CONTACT_EMAIL_PUBLIC", "maisondecafe.coffee@gmail.com")
CONTACT_PHONE_PUBLIC = os.getenv("CONTACT_PHONE_PUBLIC", "+32470600806")
TELEGRAM_CHANNEL_URL = os.getenv("TELEGRAM_CHANNEL_URL", "https://t.me/maisondecafe")

# Audio transcription model (works with OpenAI python SDK 2.x)
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")


if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID is not set")


client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# In-memory state
# =========================
user_threads: Dict[str, str] = {}
user_lang: Dict[str, str] = {}  # "ru", "uk", "en", "fr", "nl"

lead_states: Dict[str, str] = {}  # uid -> stage
lead_data: Dict[str, Dict[str, str]] = {}  # uid -> fields


# =========================
# Keyboards
# =========================
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Что такое Maison de Café?", "Сколько стоит открыть кофейню?"],
        ["Окупаемость и прибыль", "Помощь с выбором локации"],
        ["Условия франшизы", "Контакты / связь с владельцем"],
        ["Оставить заявку"],
        ["Выбрать язык"],
    ],
    resize_keyboard=True,
)

LANG_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Українська", "Русский"],
        ["English", "Français"],
        ["Nederlands"],
        ["Назад"],
    ],
    resize_keyboard=True,
)


# =========================
# Helpers
# =========================
def normalize_phone(phone: str) -> str:
    phone = phone.strip()
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    return phone


def is_valid_email(email: str) -> bool:
    email = email.strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def get_or_create_thread(user_id: str) -> str:
    if user_id in user_threads:
        return user_threads[user_id]
    thread = client.beta.threads.create()
    user_threads[user_id] = thread.id
    return thread.id


def get_lang(user_id: str) -> str:
    return user_lang.get(user_id, "ru")


def lang_label(code: str) -> str:
    mapping = {
        "ru": "Русский",
        "uk": "Українська",
        "en": "English",
        "fr": "Français",
        "nl": "Nederlands",
    }
    return mapping.get(code, "Русский")


def build_contacts_text(user_id: str) -> str:
    return (
        f"Контакты Maison de Café:\n\n"
        f"1) Email: {CONTACT_EMAIL_PUBLIC}\n"
        f"2) Телефон: {CONTACT_PHONE_PUBLIC}\n"
        f"3) Telegram-канал: {TELEGRAM_CHANNEL_URL}\n\n"
        f"Если хотите, вы можете оставить заявку здесь в боте — и наш менеджер свяжется с вами в течение 24 часов.\n"
        f"Нажмите кнопку «Оставить заявку»."
    )


async def notify_owner_telegram(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not OWNER_TELEGRAM_ID:
        return
    try:
        await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=text)
    except Exception:
        # Do not crash bot on notification failure
        return


def send_email_smtp(subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and SMTP_FROM):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = LEAD_EMAIL_TO

        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [LEAD_EMAIL_TO], msg.as_string())
        return True
    except Exception:
        return False


def lead_summary(uid: str, username: str, data: Dict[str, str]) -> str:
    return (
        "Новый лид (Maison de Café):\n\n"
        f"Telegram user_id: {uid}\n"
        f"Username: @{username if username else '-'}\n"
        f"Имя/Фамилия: {data.get('name', '-')}\n"
        f"Телефон: {data.get('phone', '-')}\n"
        f"Email: {data.get('email', '-')}\n"
        f"Сообщение: {data.get('message', '-')}\n"
        f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def ask_language(update: Update) -> None:
    txt = "Выберите язык / Оберіть мову / Choose language / Choisissez la langue / Kies taal:"
    await update.message.reply_text(txt, reply_markup=LANG_KEYBOARD)


async def set_language_by_button(update: Update, user_id: str, text: str) -> bool:
    mapping = {
        "Русский": "ru",
        "Українська": "uk",
        "English": "en",
        "Français": "fr",
        "Nederlands": "nl",
    }
    if text in mapping:
        user_lang[user_id] = mapping[text]
        await update.message.reply_text(
            f"Язык установлен: {text}\n\nМожете задавать вопросы.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True
    return False


async def start_lead_flow(update: Update, user_id: str) -> None:
    lead_states[user_id] = "name"
    lead_data[user_id] = {}
    await update.message.reply_text(
        "Оставить заявку.\n\nШаг 1/4: Напишите ваше имя и фамилию.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_lead_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message:
        return False

    user_id = str(update.effective_user.id)
    if user_id not in lead_states:
        return False

    stage = lead_states.get(user_id)
    text = (update.message.text or "").strip()

    if stage == "name":
        if len(text) < 2:
            await update.message.reply_text("Пожалуйста, укажите имя и фамилию (минимум 2 символа).")
            return True
        lead_data[user_id]["name"] = text
        lead_states[user_id] = "phone"
        await update.message.reply_text("Шаг 2/4: Укажите ваш номер телефона (например +3247... или +380...).")
        return True

    if stage == "phone":
        phone = normalize_phone(text)
        if not phone.startswith("+") or len(phone) < 8:
            await update.message.reply_text("Пожалуйста, укажите корректный телефон в международном формате, например +32470600806.")
            return True
        lead_data[user_id]["phone"] = phone
        lead_states[user_id] = "email"
        await update.message.reply_text("Шаг 3/4: Укажите ваш email.")
        return True

    if stage == "email":
        if not is_valid_email(text):
            await update.message.reply_text("Похоже, email некорректный. Введите, пожалуйста, правильный email.")
            return True
        lead_data[user_id]["email"] = text
        lead_states[user_id] = "message"
        await update.message.reply_text("Шаг 4/4: Напишите коротко ваш запрос (что хотите обсудить).")
        return True

    if stage == "message":
        if len(text) < 2:
            await update.message.reply_text("Пожалуйста, напишите коротко ваш запрос (минимум 2 символа).")
            return True
        lead_data[user_id]["message"] = text

        data = lead_data.get(user_id, {})
        username = update.effective_user.username or ""
        summary = lead_summary(user_id, username, data)

        # Notify owner via Telegram
        await notify_owner_telegram(context, summary)

        # Send email (optional)
        email_ok = send_email_smtp("Maison de Café — New Lead", summary)

        # Cleanup
        lead_states.pop(user_id, None)
        lead_data.pop(user_id, None)

        confirm = "Спасибо! Заявка отправлена. Наш менеджер свяжется с вами в течение 24 часов."
        if not email_ok and (SMTP_HOST is None or SMTP_USER is None):
            confirm += "\n\nПримечание: отправка на email не настроена (SMTP). Уведомление владельцу отправлено в Telegram."
        await update.message.reply_text(confirm, reply_markup=MAIN_KEYBOARD)
        return True

    return False


async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if not update.message or not update.message.voice:
        return None

    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(custom_path=tmp_path)

        with open(tmp_path, "rb") as f:
            # OpenAI python SDK 2.x audio transcription
            result = client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL,
                file=f,
            )
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        text = getattr(result, "text", None)
        if text:
            return text.strip()
        return None
    except Exception:
        return None


async def ask_assistant(user_id: str, user_text: str) -> str:
    thread_id = get_or_create_thread(user_id)

    # Include language hint into user message (simple + reliable)
    lang = get_lang(user_id)
    lang_hint = f"[LANG={lang_label(lang)}] "

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=lang_hint + user_text,
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
    )

    while True:
        run_status = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id,
        )
        if run_status.status == "completed":
            break
        if run_status.status in ["failed", "cancelled", "expired"]:
            return "⚠️ Произошла ошибка при обработке запроса. Попробуйте ещё раз."
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return "⚠️ Не удалось получить ответ. Попробуйте ещё раз."

    ai_reply = messages.data[0].content[0].text.value
    return ai_reply


# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    get_or_create_thread(user_id)

    welcome_text = (
        "Привет!\n"
        "Я — официальный ассистент Maison de Café.\n\n"
        "Я помогу с:\n"
        "• запуском кофейни самообслуживания\n"
        "• стоимостью комплекта и оборудования\n"
        "• окупаемостью и прибылью\n"
        "• франшизой и поддержкой Maison de Café\n\n"
        "Выберите вопрос ниже или напишите свой.\n"
        "Также можно выбрать язык кнопкой «Выбрать язык»."
    )
    await update.message.reply_text(welcome_text, reply_markup=MAIN_KEYBOARD)


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_language(update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()

    # Lead flow has priority
    if await handle_lead_flow(update, context):
        return

    # Language flow buttons
    if text == "Выбрать язык":
        await ask_language(update)
        return
    if text == "Назад":
        await update.message.reply_text("Ок. Возвращаю главное меню.", reply_markup=MAIN_KEYBOARD)
        return
    if await set_language_by_button(update, user_id, text):
        return

    # Contacts
    if text == "Контакты / связь с владельцем":
        await update.message.reply_text(build_contacts_text(user_id), reply_markup=MAIN_KEYBOARD)
        return

    # Lead start
    if text == "Оставить заявку":
        await start_lead_flow(update, user_id)
        return

    # Otherwise ask assistant
    reply = await ask_assistant(user_id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = str(update.effective_user.id)

    # If in lead flow, ask user to type (simpler + safer)
    if user_id in lead_states:
        await update.message.reply_text("Пожалуйста, ответьте текстом — сейчас идёт заполнение заявки.")
        return

    transcript = await transcribe_voice(update, context)
    if not transcript:
        await update.message.reply_text("⚠️ Не удалось распознать голосовое. Пожалуйста, попробуйте ещё раз или отправьте текст.")
        return

    # Show what we understood (optional but useful)
    await update.message.reply_text(f"Распознал(а): {transcript}")

    reply = await ask_assistant(user_id, transcript)
    await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)


# =========================
# Entry
# =========================
def main() -> None:
    print("Bot starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("language", language_cmd))

    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()


if __name__ == "__main__":
    main()
