import os
import io
import re
import time
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Dict, Optional, Tuple, Set

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
# ВАЖНО: тред отдельный на язык, чтобы English не тащил UA контекст.
# (user_id, lang) -> thread_id
user_threads: Dict[Tuple[str, str], str] = {}

# user_id -> selected lang (ua/ru/en/fr/nl)
user_lang: Dict[str, str] = {}

# Lead form state
lead_states: Dict[str, str] = {}                # user_id -> step: name/phone/email/message
lead_data: Dict[str, Dict[str, str]] = {}       # user_id -> collected fields

# Anti-spam
user_rate: Dict[str, list] = {}                 # user_id -> timestamps
blocked_users: Set[str] = set()                 # user_id blocked


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
        # ВАЖНО: убрали «не знайшов у базі» — делаем человеческий fallback
        "kb_missing": (
            "Я зрозумів ваш запит. Щоб відповісти максимально точно, мені потрібно 1–2 уточнення.\n"
            "Напишіть, будь ласка, що саме вас цікавить (місто/локація/бюджет/кількість точок), або натисніть «Залишити заявку» — і менеджер допоможе."
        ),
        "spam_stop": "⚠️ Схоже на спам. Я тимчасово не відповідаю на такі повідомлення.",
        "no_files": "Зараз я не приймаю файли/фото/документи. Напишіть питання текстом або голосом.",
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
        # ВАЖНО: убрали «не нашёл в базе» — делаем человеческий fallback
        "kb_missing": (
            "Понял ваш вопрос. Чтобы ответить максимально точно, нужно 1–2 уточнения.\n"
            "Напишите, пожалуйста, детали (город/локация/бюджет/сколько точек), либо нажмите «Оставить заявку» — и менеджер поможет."
        ),
        "spam_stop": "⚠️ Похоже на спам. Я временно не отвечаю на такие сообщения.",
        "no_files": "Сейчас я не принимаю файлы/фото/документы. Напишите вопрос текстом или голосом.",
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
        "kb_missing": (
            "Got it. To answer as accurately as possible, I need 1–2 quick clarifications.\n"
            "Please share a few details (city/location/budget/how many points), or tap “Leave a request” and a manager will help."
        ),
        "spam_stop": "⚠️ This looks like spam. I’m temporarily not responding to such messages.",
        "no_files": "Currently I don’t accept files/photos/documents. Please ask by text or voice.",
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
        "kb_missing": (
            "J’ai compris votre demande. Pour répondre au plus juste, il me manque 1–2 précisions.\n"
            "Pouvez-vous indiquer quelques détails (ville/emplacement/budget/nombre de points), ou cliquer « Laisser une demande » ?"
        ),
        "spam_stop": "⚠️ Cela ressemble à du spam. Je ne réponds temporairement pas à ce type de messages.",
        "no_files": "Je n’accepte pas les fichiers/photos/documents pour le moment. Posez la question par texte ou voix.",
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
        "kb_missing": (
            "Begrepen. Om zo precies mogelijk te antwoorden heb ik 1–2 korte verduidelijkingen nodig.\n"
            "Deel a.u.b. details (stad/locatie/budget/aantal punten), of klik “Aanvraag achterlaten”."
        ),
        "spam_stop": "⚠️ Dit lijkt op spam. Ik reageer tijdelijk niet op dit soort berichten.",
        "no_files": "Ik accepteer nu geen bestanden/foto’s/documenten. Stel je vraag via tekst of spraak.",
        "contacts_text": (
            "Contact opnemen met Maison de Café kan via:\n\n"
            "• E-mail: maisondecafe.coffee@gmail.com\n"
            "• Telefoon: +32 470 600 806\n"
            "• Telegram-kanaal: https://t.me/maisondecafe\n\n"
            "Wil je — klik “Aanvraag achterlaten”, dan nemen we binnen 24 uur contact op."
        ),
    },
}

# =========================
# BUTTON LOOKUP (per language)
# =========================
# Возвращает ("action_key", "lang_of_button") по тексту кнопки.
BUTTON_LOOKUP: Dict[str, Tuple[str, str]] = {}
for lang in LANGS:
    for key, label in MENU[lang].items():
        BUTTON_LOOKUP[label] = (key, lang)


# =========================
# HELPERS
# =========================
def get_lang(user_id: str) -> str:
    return user_lang.get(user_id, "ua")  # default UA

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
        if (text or "").strip() == label:
            return code
    return None

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

def ensure_thread(user_id: str, lang: str) -> str:
    # thread будет в ЧАСТИ 2/2
    key = (user_id, lang)
    if key not in user_threads:
        thread = client.beta.threads.create()
        user_threads[key] = thread.id
    return user_threads[key]

def reset_threads(user_id: str):
    for lang in list(LANGS):
        user_threads.pop((user_id, lang), None)

def is_gibberish_or_spam(text: str) -> bool:
    if not text:
        return False
    s = text.strip().lower()
    if len(s) <= 2:
        return True
    if re.fullmatch(r"(.)\1{6,}", s):
        return True
    letters = sum(ch.isalpha() for ch in s)
    if letters <= 2 and len(s) >= 5:
        return True
    return False

def rate_limited(user_id: str, max_per_30s: int = 8) -> bool:
    now = time.time()
    timestamps = user_rate.get(user_id, [])
    timestamps = [ts for ts in timestamps if now - ts < 30]
    timestamps.append(now)
    user_rate[user_id] = timestamps
    return len(timestamps) > max_per_30s

def button_action_from_text(text: str) -> Optional[Tuple[str, str]]:
    return BUTTON_LOOKUP.get((text or "").strip())

def is_language_button(text: str) -> bool:
    action = button_action_from_text(text)
    return bool(action and action[0] == "lang")

def is_lead_button(text: str) -> bool:
    action = button_action_from_text(text)
    return bool(action and action[0] == "lead")

def is_contacts_button(text: str) -> bool:
    action = button_action_from_text(text)
    return bool(action and action[0] == "contacts")


# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_lang.setdefault(user_id, "ua")

    await update.message.reply_text(
        TEXTS["ua"]["welcome"],
        reply_markup=mk_main_keyboard("ua"),
    )


# =========================
# LANGUAGE FLOW
# =========================
async def show_language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lang = get_lang(user_id)
    await update.message.reply_text(TEXTS[lang]["choose_lang"], reply_markup=mk_lang_keyboard())

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang_code: str):
    user_id = str(update.effective_user.id)
    user_lang[user_id] = lang_code

    await update.message.reply_text(
        TEXTS[lang_code]["lang_set"].format(lang=LANG_LABELS[lang_code]),
        reply_markup=mk_main_keyboard(lang_code),
    )


# =========================
# LEAD FORM FLOW (НЕ ТРОГАЕМ, как договорились)
# =========================
async def start_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lead_states[user_id] = "name"
    lead_data[user_id] = {}

    lang = get_lang(user_id)
    await update.message.reply_text(
        TEXTS[lang]["lead_start"],
        reply_markup=mk_main_keyboard(lang),
    )

async def handle_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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

        owner_notified = False
        if OWNER_TELEGRAM_ID:
            try:
                await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=payload)
                owner_notified = True
            except Exception as e:
                print("OWNER TG NOTIFY ERROR:", repr(e))

        email_sent = send_lead_email("Maison de Café — New lead", payload)

        if email_sent:
            email_note = "✅ Email-сповіщення відправлено."
        else:
            email_note = (
                "Примітка: відправка на email не налаштована (SMTP). Сповіщення власнику відправлено в Telegram."
                if owner_notified
                else "Примітка: email (SMTP) не налаштовано, і Telegram-сповіщення власнику не відправлено."
            )

        await update.message.reply_text(
            TEXTS[lang]["lead_done"].format(email_note=email_note),
            reply_markup=mk_main_keyboard(lang),
        )

        lead_data.pop(user_id, None)
        return True

    return False


# =========================
# ADMIN COMMANDS
# =========================
def is_owner(user_id: str) -> bool:
    return bool(OWNER_TELEGRAM_ID and user_id == str(OWNER_TELEGRAM_ID))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    lines = [
        f"Lang users: {len(user_lang)}",
        f"Threads: {len(user_threads)}",
        f"Lead states: {len(lead_states)}",
        f"Blocked: {len(blocked_users)}",
    ]
    await update.message.reply_text("\n".join(lines))

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    reset_threads(user_id)
    await update.message.reply_text("✅ Thread reset.", reply_markup=mk_main_keyboard(get_lang(user_id)))

async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /block <telegram_user_id>")
        return
    blocked_users.add(str(context.args[0]))
    await update.message.reply_text("✅ Blocked.")

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <telegram_user_id>")
        return
    blocked_users.discard(str(context.args[0]))
    await update.message.reply_text("✅ Unblocked.")


# =========================
# STRICT KB ASSISTANT (BUTTONS)
# =========================

# Командные промпты для кнопок (строго “что именно нужно ответить”)
# ВАЖНО: ответы должны быть короткие, структурированные и “человечные”.
BUTTON_PROMPTS = {
    "what": {
        "ua": "Поясни: що таке Maison de Café. Дай чітко: формат, для кого, як працює, що входить у старт, що отримує партнер. Коротко, по суті.",
        "ru": "Поясни: что такое Maison de Café. Дай чётко: формат, для кого, как работает, что входит в старт, что получает партнёр. Коротко, по сути.",
        "en": "Explain what Maison de Café is. Clearly: concept, who it is for, how it works, what is included in the start package, what the partner gets. Short and to the point.",
        "fr": "Explique ce qu’est Maison de Café : concept, pour qui, comment ça marche, ce qui est inclus au démarrage, ce que reçoit le partenaire. Court et clair.",
        "nl": "Leg uit wat Maison de Café is: concept, voor wie, hoe het werkt, wat is inbegrepen bij de start, wat de partner krijgt. Kort en duidelijk.",
    },
    "price": {
        "ua": "Відповідай про вартість відкриття. Дай структуру витрат і що входить/не входить. Якщо є діапазони — назви їх. Без загальних порад.",
        "ru": "Ответь про стоимость открытия. Дай структуру затрат и что входит/не входит. Если есть диапазоны — назови. Без общих советов.",
        "en": "Answer about opening cost. Provide cost structure and what is included/not included. If ranges exist, state them. No generic tips.",
        "fr": "Réponds sur le coût d’ouverture : structure des coûts, inclus/non inclus. Si une fourchette existe, donne-la. Pas de conseils généraux.",
        "nl": "Antwoord over opstartkosten: kostenstructuur, wat inbegrepen/niet inbegrepen is. Als er ranges zijn, noem ze. Geen algemene tips.",
    },
    "payback": {
        "ua": (
            "Відповідай тільки про окупність і прибуток. Обов’язково наведи приклад базової моделі: "
            "маржа ~1.8€/чашка, 35 чашок/день, 30 днів. Порахуй валову маржу/міс і покажи приклад витрат ~500–600€/міс "
            "та як з цього виходить чистий результат і логіка окупності. Коротко і зрозуміло."
        ),
        "ru": (
            "Отвечай только про окупаемость и прибыль. Обязательно приведи пример базовой модели: "
            "маржа ~1.8€/чашка, 35 чашек/день, 30 дней. Посчитай валовую маржу/мес и покажи пример расходов ~500–600€/мес "
            "и как из этого получается чистый результат и логика окупаемости. Коротко и понятно."
        ),
        "en": (
            "Answer ONLY about payback and profit. Must include a simple example model: "
            "~€1.8 margin per cup, 35 cups/day, 30 days. Calculate gross margin per month and show example monthly costs ~€500–€600 "
            "and how net result leads to payback logic. Short, clear."
        ),
        "fr": (
            "Réponds UNIQUEMENT sur la rentabilité et le profit. Donne un exemple simple : "
            "marge ~1,8€/tasse, 35 tasses/jour, 30 jours. Calcule la marge brute/mois et donne un exemple de coûts ~500–600€/mois "
            "et explique la logique de retour sur investissement. Court et clair."
        ),
        "nl": (
            "Antwoord ALLEEN over terugverdientijd en winst. Geef een eenvoudig voorbeeld: "
            "~€1,8 marge per kop, 35 koppen/dag, 30 dagen. Bereken bruto marge/maand en geef voorbeeldkosten ~€500–€600/maand "
            "en leg uit hoe dit tot terugverdientijd leidt. Kort en duidelijk."
        ),
    },
    "franchise": {
        "ua": "Відповідай про умови співпраці/франшизи: формат, підтримка, зобов’язання партнера, стандарти, сервіс. Без вигадок.",
        "ru": "Ответь про условия сотрудничества/франшизы: формат, поддержка, обязательства партнера, стандарты, сервис. Без выдумок.",
        "en": "Answer about franchise/partnership terms: format, support, partner obligations, standards, service. No inventions.",
        "fr": "Réponds sur les conditions franchise/partenariat : format, support, obligations, standards, service. Sans inventer.",
        "nl": "Antwoord over franchise-/samenwerkingsvoorwaarden: format, ondersteuning, verplichtingen, standaarden, service. Niet verzinnen.",
    },
}

# Глобальные строгие инструкции “только из KB” ДЛЯ КНОПОК
STRICT_KB_RULES = {
    "ua": (
        "Ти — Макс, помічник Maison de Café. "
        "КРИТИЧНО: відповідай ЛИШЕ використовуючи базу знань Maison de Café, яка підключена до цього Assistant. "
        "НЕ вигадуй і НЕ підміняй Maison de Café іншими бізнес-моделями (звичайна кав'ярня не підходить). "
        "Якщо даних недостатньо — постав 1–2 уточнюючі питання і запропонуй залишити заявку. "
        "Відповідай українською. "
        "Якщо є розрахунки — рахуй чітко й прозоро."
    ),
    "ru": (
        "Ты — Макс, помощник Maison de Café. "
        "КРИТИЧНО: отвечай ТОЛЬКО используя базу знаний Maison de Café, подключенную к этому Assistant. "
        "НЕ выдумывай и НЕ подменяй Maison de Café другими бизнес-моделями (обычная стационарная кофейня не подходит). "
        "Если данных недостаточно — задай 1–2 уточняющих вопроса и предложи оставить заявку. "
        "Отвечай на русском. "
        "Если есть расчёты — считай чётко и прозрачно."
    ),
    "en": (
        "You are Max, Maison de Café assistant. "
        "CRITICAL: answer ONLY using the Maison de Café knowledge base connected to this Assistant. "
        "Do NOT invent and do NOT replace Maison de Café with generic coffee shop advice. "
        "If details are missing, ask 1–2 clarifying questions and suggest leaving a request. "
        "Answer in English. "
        "For math questions, calculate clearly and transparently."
    ),
    "fr": (
        "Tu es Max, assistant de Maison de Café. "
        "CRITIQUE : réponds UNIQUEMENT à partir de la base de connaissances Maison de Café connectée à cet Assistant. "
        "N’invente pas et ne remplace pas Maison de Café par des conseils génériques pour une caféterie classique. "
        "S’il manque des détails, pose 1–2 questions de clarification et propose de laisser une demande. "
        "Réponds en français. "
        "Pour les calculs, calcule clairement."
    ),
    "nl": (
        "Je bent Max, assistent van Maison de Café. "
        "KRITISCH: antwoord ALLEEN met informatie uit de Maison de Café kennisbank die aan deze Assistant is gekoppeld. "
        "Niet verzinnen en geen generiek advies voor een gewone koffiebar geven. "
        "Als details ontbreken, stel 1–2 verduidelijkingsvragen en stel voor om een aanvraag achter te laten. "
        "Antwoord in het Nederlands. "
        "Bij berekeningen: reken duidelijk."
    ),
}

# =========================
# HUMAN CONSULTANT MODE (FREE TEXT)
# =========================
# Максимально человеческий тон + строго про Maison de Café + опора на KB
HUMAN_CONSULTANT_RULES = {
    "ua": (
        "Ти — Макс, живий консультант Maison de Café (проєкт кавових точок самообслуговування). "
        "Ти спілкуєшся по-людськи: спокійно, впевнено, доброзичливо, без сухих фраз. "
        "Ти відповідаєш проєктно і по суті, як менеджер з продажів/консультант. "
        "КРИТИЧНО: не підміняй Maison de Café порадами про «звичайну кав'ярню». "
        "КРИТИЧНО: не вигадуй фактів і цифр. Спирайся на базу знань Maison de Café, підключену до цього Assistant. "
        "Якщо питання про математику/окупність — рахуй чітко і прозоро. "
        "Якщо даних недостатньо, не кажи «я не знайшов у базі» — "
        "замість цього постав 1–2 уточнення і запропонуй залишити заявку, щоб менеджер підтвердив деталі."
    ),
    "ru": (
        "Ты — Макс, живой консультант Maison de Café (кофейные точки самообслуживания). "
        "Ты говоришь по-человечески: спокойно, уверенно, доброжелательно. "
        "Ты отвечаешь как консультант/продажи и ведёшь к следующему шагу. "
        "КРИТИЧНО: не подменяй Maison de Café советами про «обычную кофейню». "
        "КРИТИЧНО: не выдумывай факты и цифры. Опирайся на базу знаний Maison de Café, подключённую к этому Assistant. "
        "Если вопрос про математику/окупаемость — считай чётко и прозрачно. "
        "Если данных не хватает, не говори «я не нашёл в базе» — "
        "задай 1–2 уточнения и предложи оставить заявку, чтобы менеджер подтвердил детали."
    ),
    "en": (
        "You are Max, a human consultant for Maison de Café (self-service coffee points). "
        "You speak in a calm, confident, friendly, human way (not robotic). "
        "You answer as a sales/launch consultant and guide to the next step. "
        "CRITICAL: do not replace Maison de Café with generic coffee shop advice. "
        "CRITICAL: do not invent facts or numbers. Rely on the Maison de Café knowledge base connected to this Assistant. "
        "For math/payback questions, calculate clearly and transparently. "
        "If details are missing, do NOT say 'I couldn’t find it in the KB' — "
        "ask 1–2 clarifying questions and suggest leaving a request so a manager can confirm specifics."
    ),
    "fr": (
        "Tu es Max, consultant humain de Maison de Café (points café en libre-service). "
        "Tu parles de façon calme, confiante et naturelle. "
        "Tu réponds comme un consultant commercial et tu guides vers l’étape suivante. "
        "CRITIQUE : ne remplace pas Maison de Café par des conseils génériques pour une caféterie classique. "
        "CRITIQUE : n’invente pas de faits ni de chiffres. Appuie-toi sur la base de connaissances Maison de Café connectée à cet Assistant. "
        "Pour les calculs/rentabilité, calcule clairement. "
        "S’il manque des détails, ne dis pas 'je n’ai pas trouvé' — "
        "pose 1–2 questions et propose de laisser une demande."
    ),
    "nl": (
        "Je bent Max, een menselijke consultant van Maison de Café (zelfbedieningskoffiepunten). "
        "Je communiceert rustig, zelfverzekerd en vriendelijk (niet robotachtig). "
        "Je antwoordt als sales/launch-consultant en stuurt naar de volgende stap. "
        "KRITISCH: vervang Maison de Café niet door algemeen advies voor een gewone koffiebar. "
        "KRITISCH: verzin geen feiten of cijfers. Gebruik de Maison de Café kennisbank die aan deze Assistant is gekoppeld. "
        "Voor winst/terugverdientijd: reken duidelijk en transparant. "
        "Als details ontbreken, zeg niet 'niet gevonden' — "
        "stel 1–2 verduidelijkingsvragen en stel voor om een aanvraag achter te laten."
    ),
}


def build_instructions(lang: str, action_key: Optional[str] = None) -> str:
    """
    КНОПКИ: строгий режим (KB-only) + TASK по BUTTON_PROMPTS
    ТЕКСТ: человеческий консультант (KB-based, без выдумок, без фразы 'не нашёл')
    """
    # КНОПКИ = строгий режим
    if action_key and action_key in BUTTON_PROMPTS:
        base = STRICT_KB_RULES.get(lang, STRICT_KB_RULES["ua"])
        return base + "\n\nTASK:\n" + BUTTON_PROMPTS[action_key][lang]

    # СВОБОДНЫЙ ДИАЛОГ = человеческий режим
    return HUMAN_CONSULTANT_RULES.get(lang, HUMAN_CONSULTANT_RULES["ua"])


async def ask_assistant_strict(user_id: str, lang: str, user_text: str, action_key: Optional[str] = None) -> str:
    """
    Всегда:
    - thread = (user_id, lang) чтобы контекст не мешал языкам
    - instructions = строгие KB (для кнопок) или human consultant (для текста)
    """
    thread_id = ensure_thread(user_id, lang)

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text,
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        instructions=build_instructions(lang, action_key),
    )

    while True:
        rs = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if rs.status == "completed":
            break
        if rs.status in ["failed", "cancelled", "expired"]:
            return ""
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return ""
    # последнее сообщение ассистента обычно первым в списке
    return messages.data[0].content[0].text.value


def looks_like_kb_missing(ai_reply: str, lang: str) -> bool:
    """
    Мягкая страховка: мы больше НЕ режем «человечные» ответы.
    Возвращаем True только если:
    - ответ пустой
    - или ассистент явно пишет 'не нашёл/не найдено'
    """
    if not ai_reply:
        return True

    low = ai_reply.strip().lower()

    hard_fail_markers = [
        "я не знайшов",
        "я не нашел",
        "не нашёл",
        "i couldn’t find",
        "i could not find",
        "not found in the",
        "je n’ai pas trouvé",
        "je n'ai pas trouvé",
        "pas trouvé dans",
        "ik kon het niet vinden",
        "niet gevonden in",
    ]
    return any(m in low for m in hard_fail_markers)


# =========================
# NON-TEXT (FILES, PHOTOS) - BLOCK
# =========================
async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lang = get_lang(user_id)
    await update.message.reply_text(TEXTS[lang]["no_files"], reply_markup=mk_main_keyboard(lang))
# =========================
# VOICE HANDLER
# =========================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id in blocked_users:
        return

    if rate_limited(user_id) or is_gibberish_or_spam("voice"):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["spam_stop"], reply_markup=mk_main_keyboard(lang))
        return

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

        # если пользователь в лид-форме — голос считается вводом шага
        if user_id in lead_states:
            update.message.text = user_text
            handled = await handle_lead_form(update, context)
            if handled:
                return

        # иначе — ассистент (human mode for free text)
        ai_reply = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=user_text, action_key=None)

        if looks_like_kb_missing(ai_reply, lang):
            await update.message.reply_text(TEXTS[lang]["kb_missing"], reply_markup=mk_main_keyboard(lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))

    except Exception as e:
        print("VOICE ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))


# =========================
# TEXT ROUTER (MAIN)
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()

    # BLOCKED
    if user_id in blocked_users:
        return

    # антиспам / rate limit
    if is_gibberish_or_spam(text) or rate_limited(user_id):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["spam_stop"], reply_markup=mk_main_keyboard(lang))
        return

    # если в лид-форме — приоритет лид-формы
    if user_id in lead_states:
        handled = await handle_lead_form(update, context)
        if handled:
            return

    # язык меню
    if is_language_button(text):
        await show_language_menu(update, context)
        return

    chosen = parse_lang_choice(text)
    if chosen:
        await set_language(update, context, chosen)
        return

    # определяем: это кнопка?
    action = button_action_from_text(text)

    # Контакты — статические (не через ассистент)
    if is_contacts_button(text):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    # Лид-форма — локальная логика (мы её не меняем)
    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # Если нажата одна из “контентных” кнопок (what/price/payback/franchise):
    if action and action[0] in {"what", "price", "payback", "franchise"}:
        action_key, button_lang = action

        # ЖЁСТКО: язык = язык кнопки
        user_lang[user_id] = button_lang

        # Текст для ассистента — команда (не просто текст кнопки)
        command_text = f"[BUTTON:{action_key}] {MENU[button_lang][action_key]}"

        ai_reply = await ask_assistant_strict(
            user_id=user_id,
            lang=button_lang,
            user_text=command_text,
            action_key=action_key,
        )

        if looks_like_kb_missing(ai_reply, button_lang):
            await update.message.reply_text(TEXTS[button_lang]["kb_missing"], reply_markup=mk_main_keyboard(button_lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(button_lang))
        return

    # Иначе: это обычный вопрос пользователя (human consultant mode).
    lang = get_lang(user_id)
    try:
        ai_reply = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=text, action_key=None)

        if looks_like_kb_missing(ai_reply, lang):
            await update.message.reply_text(TEXTS[lang]["kb_missing"], reply_markup=mk_main_keyboard(lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))

    except Exception as e:
        print("ASSISTANT ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))


# =========================
# ENTRYPOINT
# =========================
def main():
    print("🚀 Bot is starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))

    # voice BEFORE text
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # non-text блокируем (фото/док/видео/аудио/стикер/анимации/и т.п.)
    # ВАЖНО: НИКАКИХ filters.STICKER тут нет — это и было твоей ошибкой из лога.
    application.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.VIDEO_NOTE
            | filters.ANIMATION
            | filters.CONTACT
            | filters.LOCATION,
            handle_non_text,
        )
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
