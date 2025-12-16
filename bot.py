import os
import io
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Dict, Optional

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

# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")  # обязательный для уведомлений владельцу
LEAD_EMAIL_TO = os.getenv("LEAD_EMAIL_TO", "maisondecafe.coffee@gmail.com")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID не задан в переменных окружения")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# STATE (IN-MEMORY)
# =========================
user_threads: Dict[str, str] = {}   # user_id -> thread_id
user_lang: Dict[str, str] = {}      # user_id -> lang (ua/ru/en/fr/nl)

lead_states: Dict[str, str] = {}    # user_id -> step: name/phone/email/message
lead_data: Dict[str, Dict[str, str]] = {}  # user_id -> collected fields

# =========================
# I18N (texts + buttons)
# =========================
LANGS = ["ua", "ru", "en", "fr", "nl"]

LANG_LABELS = {
    "ua": "🇺🇦 Українська",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "fr": "🇫🇷 Français",
    "nl": "🇳🇱 Nederlands",
}

# Главное меню (кнопки) — локализовано
MENU = {
    "ua": {
        "what": "☕ Що таке Maison de Café?",
        "price": "💶 Скільки коштує відкрити кав’ярню?",
        "payback": "📈 Окупність і прибуток",
        "franchise": "🤝 Умови франшизи",
        "contacts": "📞 Контакти / зв’язок з власником",
        "lead": "📝 Залишити заявку",
        "lang": "🌍 Мова / Language",
    },
    "ru": {
        "what": "☕ Что такое Maison de Café?",
        "price": "💶 Сколько стоит открыть кофейню?",
        "payback": "📈 Окупаемость и прибыль",
        "franchise": "🤝 Условия франшизы",
        "contacts": "📞 Контакты / связь с владельцем",
        "lead": "📝 Оставить заявку",
        "lang": "🌍 Язык / Language",
    },
    "en": {
        "what": "☕ What is Maison de Café?",
        "price": "💶 How much does it cost to open a coffee point?",
        "payback": "📈 Payback & profit",
        "franchise": "🤝 Franchise terms",
        "contacts": "📞 Contacts / owner",
        "lead": "📝 Leave a request",
        "lang": "🌍 Language",
    },
    "fr": {
        "what": "☕ Qu’est-ce que Maison de Café ?",
        "price": "💶 Combien coûte l’ouverture ?",
        "payback": "📈 Rentabilité & profit",
        "franchise": "🤝 Conditions de franchise",
        "contacts": "📞 Contacts / propriétaire",
        "lead": "📝 Laisser une demande",
        "lang": "🌍 Langue / Language",
    },
    "nl": {
        "what": "☕ Wat is Maison de Café?",
        "price": "💶 Wat kost het om te starten?",
        "payback": "📈 Terugverdientijd & winst",
        "franchise": "🤝 Franchisevoorwaarden",
        "contacts": "📞 Contact / eigenaar",
        "lead": "📝 Aanvraag achterlaten",
        "lang": "🌍 Taal / Language",
    },
}

TEXTS = {
    "ua": {
        "welcome": (
            "Добрий день!\n"
            "Мене звати Макс, я віртуальний помічник компанії Maison de Café.\n"
            "Я допоможу вам розібратися у всіх питаннях, пов’язаних з нашими кав’ярнями самообслуговування, запуском і умовами співпраці.\n"
            "Щоб продовжити, підкажіть, будь ласка, як вас звати?"
        ),
        "choose_lang": "🌍 Оберіть мову:",
        "lang_set": "✅ Мову змінено: {lang}.",
        "lead_start": "📝 Залишити заявку.\n\nКрок 1/4: Напишіть ваше ім’я та прізвище.",
        "lead_phone": "Крок 2/4: Напишіть ваш номер телефону.",
        "lead_email": "Крок 3/4: Напишіть ваш email.",
        "lead_msg": "Крок 4/4: Коротко опишіть ваш запит (1–2 речення).",
        "lead_done": (
            "Дякуємо! Заявку відправлено. Наш менеджер зв’яжеться з вами протягом 24 годин.\n\n"
            "{email_note}"
        ),
        "voice_fail": "Не вдалося розпізнати голос. Спробуйте ще раз.",
        "generic_error": "⚠️ Сталася помилка. Спробуйте ще раз.",
        "contacts_text": (
            "Зв’язатися з Maison de Café можна так:\n\n"
            "• Email: maisondecafe.coffee@gmail.com\n"
            "• Телефон: +32 470 600 806\n"
            "• Telegram-канал: https://t.me/maisondecafe\n\n"
            "Якщо хочете — натисніть «Залишити заявку», і менеджер зв’яжеться з вами протягом 24 годин."
        ),
    },
    "ru": {
        "welcome": (
            "Добрый день!\n"
            "Меня зовут Макс, я виртуальный помощник компании Maison de Café.\n"
            "Я помогу вам разобраться во всех вопросах, связанных с нашими кофейнями самообслуживания, запуском и условиями сотрудничества.\n"
            "Чтобы продолжить, подскажите, пожалуйста, как вас зовут?"
        ),
        "choose_lang": "🌍 Выберите язык:",
        "lang_set": "✅ Язык установлен: {lang}.",
        "lead_start": "📝 Оставить заявку.\n\nШаг 1/4: Напишите ваше имя и фамилию.",
        "lead_phone": "Шаг 2/4: Напишите ваш номер телефона.",
        "lead_email": "Шаг 3/4: Напишите ваш email.",
        "lead_msg": "Шаг 4/4: Коротко опишите запрос (1–2 предложения).",
        "lead_done": (
            "Спасибо! Заявка отправлена. Наш менеджер свяжется с вами в течение 24 часов.\n\n"
            "{email_note}"
        ),
        "voice_fail": "Не удалось распознать голос. Попробуйте ещё раз.",
        "generic_error": "⚠️ Произошла ошибка. Попробуйте ещё раз.",
        "contacts_text": (
            "Связаться с Maison de Café можно так:\n\n"
            "• Email: maisondecafe.coffee@gmail.com\n"
            "• Телефон: +32 470 600 806\n"
            "• Telegram-канал: https://t.me/maisondecafe\n\n"
            "Если хотите — нажмите «Оставить заявку», и менеджер свяжется с вами в течение 24 часов."
        ),
    },
    "en": {
        "welcome": (
            "Hello!\n"
            "My name is Max, I’m the virtual assistant of Maison de Café.\n"
            "I’ll help you with everything related to our self-service coffee points, launch costs, and partnership terms.\n"
            "To continue, may I know your name?"
        ),
        "choose_lang": "🌍 Choose a language:",
        "lang_set": "✅ Language set: {lang}.",
        "lead_start": "📝 Leave a request.\n\nStep 1/4: Please type your first & last name.",
        "lead_phone": "Step 2/4: Please type your phone number.",
        "lead_email": "Step 3/4: Please type your email.",
        "lead_msg": "Step 4/4: Briefly describe your request (1–2 sentences).",
        "lead_done": "Thank you! Request sent. Our manager will contact you within 24 hours.\n\n{email_note}",
        "voice_fail": "I couldn't understand the voice message. Please try again.",
        "generic_error": "⚠️ Something went wrong. Please try again.",
        "contacts_text": (
            "You can contact Maison de Café via:\n\n"
            "• Email: maisondecafe.coffee@gmail.com\n"
            "• Phone: +32 470 600 806\n"
            "• Telegram channel: https://t.me/maisondecafe\n\n"
            "If you want — tap “Leave a request” and a manager will contact you within 24 hours."
        ),
    },
    "fr": {
        "welcome": (
            "Bonjour !\n"
            "Je m’appelle Max, assistant virtuel de Maison de Café.\n"
            "Je peux vous aider sur le lancement, les coûts et les conditions de partenariat.\n"
            "Pour continuer, comment vous appelez-vous ?"
        ),
        "choose_lang": "🌍 Choisissez la langue :",
        "lang_set": "✅ Langue sélectionnée : {lang}.",
        "lead_start": "📝 Laisser une demande.\n\nÉtape 1/4 : votre nom et prénom.",
        "lead_phone": "Étape 2/4 : votre numéro de téléphone.",
        "lead_email": "Étape 3/4 : votre email.",
        "lead_msg": "Étape 4/4 : décrivez brièvement votre demande (1–2 phrases).",
        "lead_done": "Merci ! Demande envoyée. Un manager vous contactera sous 24h.\n\n{email_note}",
        "voice_fail": "Je n’ai pas pu comprendre le message vocal. Réessayez.",
        "generic_error": "⚠️ Une erreur est survenue. Réessayez.",
        "contacts_text": (
            "Vous pouvez contacter Maison de Café via :\n\n"
            "• Email : maisondecafe.coffee@gmail.com\n"
            "• Téléphone : +32 470 600 806\n"
            "• Canal Telegram : https://t.me/maisondecafe\n\n"
            "Si vous voulez — cliquez « Laisser une demande » et un manager vous contactera sous 24h."
        ),
    },
    "nl": {
        "welcome": (
            "Hallo!\n"
            "Ik ben Max, de virtuele assistent van Maison de Café.\n"
            "Ik help je met vragen over startkosten, winst en franchisevoorwaarden.\n"
            "Om verder te gaan: hoe heet je?"
        ),
        "choose_lang": "🌍 Kies een taal:",
        "lang_set": "✅ Taal ingesteld: {lang}.",
        "lead_start": "📝 Aanvraag achterlaten.\n\nStap 1/4: Typ je voor- en achternaam.",
        "lead_phone": "Stap 2/4: Typ je telefoonnummer.",
        "lead_email": "Stap 3/4: Typ je e-mail.",
        "lead_msg": "Stap 4/4: Beschrijf kort je vraag (1–2 zinnen).",
        "lead_done": "Bedankt! Aanvraag verzonden. We nemen binnen 24 uur contact op.\n\n{email_note}",
        "voice_fail": "Ik kon het spraakbericht niet begrijpen. Probeer het opnieuw.",
        "generic_error": "⚠️ Er ging iets mis. Probeer het opnieuw.",
        "contacts_text": (
            "Contact opnemen met Maison de Café kan via:\n\n"
            "• E-mail: maisondecafe.coffee@gmail.com\n"
            "• Telefoon: +32 470 600 806\n"
            "• Telegram-kanaal: https://t.me/maisondecafe\n\n"
            "Wil je — klik “Aanvraag achterlaten”, dan nemen we binnen 24 uur contact op."
        ),
    },
}

ASSISTANT_LANG_INSTRUCTIONS = {
    "ua": "Відповідай українською мовою. Якщо користувач пише іншою мовою — все одно відповідай українською.",
    "ru": "Отвечай на русском языке.",
    "en": "Respond in English.",
    "fr": "Réponds en français.",
    "nl": "Antwoord in het Nederlands.",
}

# =========================
# HELPERS
# =========================
def get_lang(user_id: str) -> str:
    return user_lang.get(user_id, "ua")  # по умолчанию украинский

def t(user_id: str, key: str) -> str:
    lang = get_lang(user_id)
    return TEXTS.get(lang, TEXTS["ua"]).get(key, TEXTS["ua"].get(key, ""))

def mk_main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    m = MENU[lang]
    kb = [
        [m["what"], m["price"]],
        [m["payback"], m["franchise"]],
        [m["contacts"], m["lead"]],
        [m["lang"]],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def mk_lang_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [LANG_LABELS["ua"], LANG_LABELS["ru"]],
        [LANG_LABELS["en"], LANG_LABELS["fr"]],
        [LANG_LABELS["nl"]],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)

def parse_lang_choice(text: str) -> Optional[str]:
    for code, label in LANG_LABELS.items():
        if text.strip() == label:
            return code
    return None

def is_lang_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["lang"] for l in LANGS}

def is_lead_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["lead"] for l in LANGS}

def is_contacts_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["contacts"] for l in LANGS}

def ensure_thread(user_id: str) -> str:
    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id
    return user_threads[user_id]

def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and SMTP_FROM and LEAD_EMAIL_TO)

def send_lead_email(subject: str, body: str) -> bool:
    if not smtp_configured():
        return False

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = LEAD_EMAIL_TO

        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [LEAD_EMAIL_TO], msg.as_string())
        return True
    except Exception as e:
        print("SMTP ERROR:", repr(e))
        return False

# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # default language UA
    if user_id not in user_lang:
        user_lang[user_id] = "ua"

    # create thread for user
    ensure_thread(user_id)

    lang = get_lang(user_id)
    await update.message.reply_text(
        TEXTS[lang]["welcome"],
        reply_markup=mk_main_keyboard(lang),
    )

# =========================
# LANGUAGE FLOW
# =========================
async def show_language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    await update.message.reply_text(t(user_id, "choose_lang"), reply_markup=mk_lang_keyboard())

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang_code: str):
    user_id = str(update.effective_user.id)
    user_lang[user_id] = lang_code

    await update.message.reply_text(
        t(user_id, "lang_set").format(lang=LANG_LABELS[lang_code]),
        reply_markup=mk_main_keyboard(lang_code),
    )

# =========================
# LEAD FORM FLOW
# =========================
async def start_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lead_states[user_id] = "name"
    lead_data[user_id] = {}

    await update.message.reply_text(
        t(user_id, "lead_start"),
        reply_markup=mk_main_keyboard(get_lang(user_id)),
    )

async def handle_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lang = get_lang(user_id)

    step = lead_states.get(user_id)
    text = (update.message.text or "").strip()

    if not step:
        return False

    if step == "name":
        lead_data[user_id]["name"] = text
        lead_states[user_id] = "phone"
        await update.message.reply_text(TEXTS[lang]["lead_phone"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "phone":
        lead_data[user_id]["phone"] = text
        lead_states[user_id] = "email"
        await update.message.reply_text(TEXTS[lang]["lead_email"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "email":
        lead_data[user_id]["email"] = text
        lead_states[user_id] = "message"
        await update.message.reply_text(TEXTS[lang]["lead_msg"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "message":
        lead_data[user_id]["message"] = text
        lead_states.pop(user_id, None)

        # Prepare lead payload
        username = update.effective_user.username or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        payload = (
            f"Telegram user_id: {user_id}\n"
            f"Username: @{username}\n"
            f"Ім'я/Прізвище: {lead_data[user_id].get('name','')}\n"
            f"Телефон: {lead_data[user_id].get('phone','')}\n"
            f"Email: {lead_data[user_id].get('email','')}\n"
            f"Повідомлення: {lead_data[user_id].get('message','')}\n"
            f"Час: {now}\n"
        )

        # Notify owner in Telegram
        owner_notified = False
        if OWNER_TELEGRAM_ID:
            try:
                await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=payload)
                owner_notified = True
            except Exception as e:
                print("OWNER TG NOTIFY ERROR:", repr(e))

        # Optional email
        email_sent = send_lead_email("Maison de Café — New lead", payload)

        if email_sent:
            email_note = "✅ Email-сповіщення відправлено."
        else:
            email_note = "Примітка: відправка на email не налаштована (SMTP). Сповіщення власнику відправлено в Telegram." if owner_notified else "Примітка: email (SMTP) не налаштовано, і Telegram-сповіщення власнику не відправлено."

        await update.message.reply_text(
            TEXTS[lang]["lead_done"].format(email_note=email_note),
            reply_markup=mk_main_keyboard(lang),
        )

        lead_data.pop(user_id, None)
        return True

    return False

# =========================
# ASSISTANT (text)
# =========================
async def ask_assistant(user_id: str, user_text: str) -> str:
    thread_id = ensure_thread(user_id)
    lang = get_lang(user_id)

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text,
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        instructions=ASSISTANT_LANG_INSTRUCTIONS.get(lang, ASSISTANT_LANG_INSTRUCTIONS["ua"]),
    )

    # wait completion
    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == "completed":
            break
        if run_status.status in ["failed", "cancelled", "expired"]:
            return ""
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return ""

    return messages.data[0].content[0].text.value

# =========================
# TEXT HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lang = get_lang(user_id)
    text = (update.message.text or "").strip()

    # Lead form step processing (priority)
    if user_id in lead_states:
        handled = await handle_lead_form(update, context)
        if handled:
            return

    # Open language menu
    if is_lang_button(text):
        await show_language_menu(update, context)
        return

    # Choose language
    chosen = parse_lang_choice(text)
    if chosen:
        await set_language(update, context, chosen)
        return

    # Lead form start
    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # Contacts (static)
    if is_contacts_button(text):
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    # Otherwise -> assistant
    try:
        ai_reply = await ask_assistant(user_id, text)
        if not ai_reply:
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return
        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))
    except Exception as e:
        print("ASSISTANT ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))

# =========================
# VOICE HANDLER
# =========================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lang = get_lang(user_id)

    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)

        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        buf.seek(0)
        buf.name = "voice.ogg"

        transcript = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=buf,
        )
        user_text = (transcript.text or "").strip()

        if not user_text:
            await update.message.reply_text(TEXTS[lang]["voice_fail"], reply_markup=mk_main_keyboard(lang))
            return

        # Если пользователь был в лид-форме — считаем транскрипт как ввод в лид-форму
        if user_id in lead_states:
            # Подменяем текст и обрабатываем как текст
            update.message.text = user_text
            await handle_message(update, context)
            return

        # обычный поток: отправляем транскрипт в ассистент
        ai_reply = await ask_assistant(user_id, user_text)
        if not ai_reply:
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))

    except Exception as e:
        print("VOICE ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))

# =========================
# ENTRYPOINT
# =========================
def main():
    print("🚀 Bot is starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    # voice must be BEFORE generic text (not strictly required, но так надежнее)
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
