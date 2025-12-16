import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# =========================================================
# Maison de Café — Telegram Bot (Render + GitHub + OpenAI Assistant)
#
# Что нужно в Render → Environment:
# 1) TELEGRAM_BOT_TOKEN   — токен из BotFather
# 2) OPENAI_API_KEY       — ключ OpenAI
# 3) ASSISTANT_ID         — ID ассистента OpenAI (где System Instructions + Files/Search + VectorStore)
# 4) OWNER_TELEGRAM_ID    — твой Telegram user id (чтобы бот присылал лиды владельцу)
#
# По умолчанию бот стартует на украинском языке.
# Меню и приветствие Макса — украинские.
# =========================================================

# ====== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")  # можно не задавать, но лиды владельцу тогда не отправятся

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID не задан в переменных окружения")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================================================
# Хранилища состояния (в памяти процесса)
# =========================================================

# У каждого пользователя — свой thread OpenAI Assistant
user_threads: dict[str, str] = {}

# Выбранный язык пользователя (по умолчанию украинский)
# "uk", "ru", "en", "fr", "nl"
user_lang: dict[str, str] = {}

# Простая FSM для формы лида
# lead_state[user_id] = {"step": int, "data": {...}}
lead_state: dict[str, dict] = {}

# =========================================================
# Константы: контакты Maison de Café
# =========================================================
CONTACT_EMAIL = "maisondecafe.coffee@gmail.com"
CONTACT_PHONE = "+32 470 600 806"
TELEGRAM_CHANNEL = "https://t.me/maisondecafe"

# =========================================================
# Кнопки: главное меню (UA)
# =========================================================
MAIN_KEYBOARD_UA = ReplyKeyboardMarkup(
    [
        ["☕ Що таке Maison de Café?", "💶 Скільки коштує відкрити кав’ярню?"],
        ["📈 Окупність і прибуток", "🤝 Умови франшизи"],
        ["📞 Контакти / зв’язок з власником", "📝 Залишити заявку"],
        ["🌍 Мова / Language"],
    ],
    resize_keyboard=True
)

# Кнопки выбора языка (коротко и понятно)
LANG_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🇺🇦 Українська", "🇷🇺 Русский", "🇬🇧 English"],
        ["🇫🇷 Français", "🇳🇱 Nederlands"],
        ["⬅️ Назад до меню"],
    ],
    resize_keyboard=True
)

# =========================================================
# Тексты (минимально необходимые)
# =========================================================

WELCOME_UA = (
    "Вітаю!\n"
    "Мене звати Макс — я віртуальний помічник компанії Maison de Café.\n"
    "Я допоможу вам розібратися з усіма питаннями щодо наших кав’ярень самообслуговування, запуску та умов співпраці.\n\n"
    "Щоб продовжити, підкажіть, будь ласка, як вас звати?"
)

LANG_INFO_UA = (
    "🌍 Оберіть мову. За замовчуванням бот працює українською.\n"
    "Ви можете змінити мову у будь-який момент через кнопку «🌍 Мова / Language»."
)

CONTACTS_UA = (
    "📞 Контакти Maison de Café:\n\n"
    f"📧 Email: {CONTACT_EMAIL}\n"
    f"☎️ Телефон: {CONTACT_PHONE}\n"
    f"🔗 Telegram-канал: {TELEGRAM_CHANNEL}\n\n"
    "Якщо бажаєте, ви можете залишити заявку — і наш менеджер зв’яжеться з вами протягом 24 годин."
)

LEAD_INTRO_UA = (
    "📝 Залишити заявку\n\n"
    "Я задам кілька коротких питань і передам заявку менеджеру.\n"
    "Почнемо.\n\n"
    "1/5 — Ваше ім’я?"
)

LEAD_CANCEL_UA = "Заявку скасовано. Повертаю вас до меню."
LEAD_DONE_UA = (
    "Дякую! ✅ Заявку прийнято.\n"
    "Наш менеджер зв’яжеться з вами протягом 24 годин.\n\n"
    f"Якщо потрібно — контакти:\n📧 {CONTACT_EMAIL}\n☎️ {CONTACT_PHONE}\n🔗 {TELEGRAM_CHANNEL}"
)

ERROR_UA = "⚠️ Сталася помилка. Спробуйте ще раз, будь ласка."
AI_ERROR_UA = "⚠️ Помилка під час обробки запиту. Спробуйте ще раз."

# =========================================================
# Вспомогательные функции
# =========================================================

def get_or_create_thread(user_id: str) -> str:
    """Получить thread_id для пользователя или создать новый."""
    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id
    return user_threads[user_id]

def get_user_language(user_id: str) -> str:
    """Получить язык пользователя (по умолчанию uk)."""
    return user_lang.get(user_id, "uk")

def set_user_language(user_id: str, lang_code: str) -> None:
    """Установить язык пользователя."""
    user_lang[user_id] = lang_code

def format_lead_message(lead: dict) -> str:
    """Сформировать сообщение владельцу по лиду."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "🆕 Новий лід (Maison de Café)\n"
        f"⏱ Час: {ts}\n\n"
        f"Ім’я: {lead.get('first_name','')}\n"
        f"Прізвище: {lead.get('last_name','')}\n"
        f"Телефон: {lead.get('phone','')}\n"
        f"Email: {lead.get('email','')}\n"
        f"Запит: {lead.get('note','')}\n"
    )

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Отправить владельцу уведомление в Telegram (если OWNER_TELEGRAM_ID задан)."""
    if not OWNER_TELEGRAM_ID:
        return
    try:
        await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=text)
    except Exception:
        # Не валим бот, если владельцу не отправилось
        pass

# =========================================================
# /start
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Создаём thread заранее
    get_or_create_thread(user_id)

    # По умолчанию — украинский
    if user_id not in user_lang:
        set_user_language(user_id, "uk")

    # Приветствие Макса на украинском + главное меню украинское
    await update.message.reply_text(WELCOME_UA, reply_markup=MAIN_KEYBOARD_UA)
    await update.message.reply_text(LANG_INFO_UA, reply_markup=MAIN_KEYBOARD_UA)

# =========================================================
# Команда /language (дополнительно, если нужно)
# =========================================================
async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌍 Оберіть мову:", reply_markup=LANG_KEYBOARD)

# =========================================================
# Запуск формы лида
# =========================================================
async def start_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lead_state[user_id] = {"step": 1, "data": {}}
    await update.message.reply_text(LEAD_INTRO_UA, reply_markup=ReplyKeyboardRemove())

async def cancel_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in lead_state:
        del lead_state[user_id]
    await update.message.reply_text(LEAD_CANCEL_UA, reply_markup=MAIN_KEYBOARD_UA)

# =========================================================
# Обработка текста в режиме лида
# =========================================================
async def handle_lead_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Возвращает True, если сообщение обработано как часть lead-формы.
    False — если не в режиме формы.
    """
    user_id = str(update.effective_user.id)
    if user_id not in lead_state:
        return False

    text = (update.message.text or "").strip()
    st = lead_state[user_id]
    step = st.get("step", 1)
    data = st.get("data", {})

    # Возможность отмены
    if text.lower() in ["скасувати", "отмена", "cancel", "/cancel"]:
        await cancel_lead_form(update, context)
        return True

    # Шаги формы: 1 имя, 2 фамилия, 3 телефон, 4 email, 5 запрос
    if step == 1:
        data["first_name"] = text
        st["step"] = 2
        await update.message.reply_text("2/5 — Ваше прізвище?")
        return True

    if step == 2:
        data["last_name"] = text
        st["step"] = 3
        await update.message.reply_text("3/5 — Ваш номер телефону (у міжнародному форматі, напр. +32...) ?")
        return True

    if step == 3:
        data["phone"] = text
        st["step"] = 4
        await update.message.reply_text("4/5 — Ваш email?")
        return True

    if step == 4:
        data["email"] = text
        st["step"] = 5
        await update.message.reply_text("5/5 — Коротко опишіть ваш запит (1–2 речення):")
        return True

    if step == 5:
        data["note"] = text

        # Отправляем владельцу в Telegram
        owner_text = format_lead_message(data)
        await notify_owner(context, owner_text)

        # Завершаем
        del lead_state[user_id]
        await update.message.reply_text(LEAD_DONE_UA, reply_markup=MAIN_KEYBOARD_UA)
        return True

    # На всякий случай
    await update.message.reply_text(ERROR_UA, reply_markup=MAIN_KEYBOARD_UA)
    return True

# =========================================================
# Основной обработчик сообщений (текст)
# =========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = (update.message.text or "").strip()

    # 1) Если пользователь в форме лида — обрабатываем форму
    if await handle_lead_flow(update, context):
        return

    # 2) Обработка кнопок меню (UA)
    if user_text == "🌍 Мова / Language":
        await update.message.reply_text("🌍 Оберіть мову:", reply_markup=LANG_KEYBOARD)
        return

    if user_text in ["⬅️ Назад до меню", "Назад", "Back"]:
        await update.message.reply_text("Готово. Повертаю вас до меню.", reply_markup=MAIN_KEYBOARD_UA)
        return

    # Выбор языка
    if user_text == "🇺🇦 Українська":
        set_user_language(user_id, "uk")
        await update.message.reply_text("✅ Мову змінено: Українська.", reply_markup=MAIN_KEYBOARD_UA)
        return

    if user_text == "🇷🇺 Русский":
        set_user_language(user_id, "ru")
        await update.message.reply_text("✅ Язык изменён: Русский.", reply_markup=MAIN_KEYBOARD_UA)
        return

    if user_text == "🇬🇧 English":
        set_user_language(user_id, "en")
        await update.message.reply_text("✅ Language set: English.", reply_markup=MAIN_KEYBOARD_UA)
        return

    if user_text == "🇫🇷 Français":
        set_user_language(user_id, "fr")
        await update.message.reply_text("✅ Langue définie : Français.", reply_markup=MAIN_KEYBOARD_UA)
        return

    if user_text == "🇳🇱 Nederlands":
        set_user_language(user_id, "nl")
        await update.message.reply_text("✅ Taal ingesteld: Nederlands.", reply_markup=MAIN_KEYBOARD_UA)
        return

    # Контакты
    if user_text == "📞 Контакти / зв’язок з власником":
        await update.message.reply_text(CONTACTS_UA, reply_markup=MAIN_KEYBOARD_UA)
        return

    # Лид-форма
    if user_text == "📝 Залишити заявку":
        await start_lead_form(update, context)
        return

    # 3) Всё остальное — передаём в OpenAI Assistant
    thread_id = get_or_create_thread(user_id)
    lang = get_user_language(user_id)

    # Подсказка ассистенту о языке (чтобы он отвечал на выбранном языке)
    # Это не заменяет System Instructions, а мягко направляет ответы.
    language_hint = {
        "uk": "Відповідай українською мовою.",
        "ru": "Отвечай на русском языке.",
        "en": "Reply in English.",
        "fr": "Réponds en français.",
        "nl": "Antwoord in het Nederlands.",
    }.get(lang, "Відповідай українською мовою.")

    try:
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"{language_hint}\n\nКористувач: {user_text}",
        )

        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID,
        )

        # Ждём завершения
        while True:
            run_status = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id,
            )
            if run_status.status == "completed":
                break
            if run_status.status in ["failed", "cancelled", "expired"]:
                await update.message.reply_text(AI_ERROR_UA, reply_markup=MAIN_KEYBOARD_UA)
                return
            await asyncio.sleep(1)

        messages = client.beta.threads.messages.list(thread_id=thread_id)
        if not messages.data:
            await update.message.reply_text(AI_ERROR_UA, reply_markup=MAIN_KEYBOARD_UA)
            return

        ai_reply = messages.data[0].content[0].text.value
        await update.message.reply_text(ai_reply, reply_markup=MAIN_KEYBOARD_UA)

    except Exception:
        await update.message.reply_text(ERROR_UA, reply_markup=MAIN_KEYBOARD_UA)

# =========================================================
# /cancel — отмена формы лида (если пользователь застрял)
# =========================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cancel_lead_form(update, context)

# =========================================================
# Точка входа
# =========================================================
def main():
    print("🚀 Maison de Café bot starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("cancel", cancel))

    # Text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # IMPORTANT:
    # Мы используем polling.
    # Если увидишь ошибку Conflict: terminated by other getUpdates request —
    # значит где-то запущен второй экземпляр бота с тем же токеном.
    application.run_polling()

if __name__ == "__main__":
    main()
