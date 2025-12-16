import os
import io
import re
import asyncio
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Dict, Optional, List, Tuple

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

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")  # for admin + lead notifications
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

# --- anti-spam state ---
user_msg_times: Dict[str, List[datetime]] = {}     # user_id -> timestamps of recent messages
user_last_text: Dict[str, str] = {}                # user_id -> last message (text after normalize)
user_repeat_count: Dict[str, int] = {}             # user_id -> repeats
user_spam_strikes: Dict[str, int] = {}             # user_id -> strikes
user_cooldown_until: Dict[str, datetime] = {}      # user_id -> ignore until

# --- admin moderation ---
banned_users: Dict[str, datetime] = {}             # user_id -> banned_until (datetime.max for permanent)

# --- debug ---
user_last_debug: Dict[str, str] = {}               # user_id -> last debug line


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

# MENU buttons (localized)
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
        "spam_warn_1": "Схоже, це не запитання 🙂 Я із задоволенням допоможу, якщо напишете конкретніше про Maison de Café.",
        "spam_warn_2": "Я можу відповідати лише на осмислені запити, пов’язані з Maison de Café. Напишіть, будь ласка, що саме вас цікавить.",
        "cooldown_msg": "Я тимчасово призупиняю відповіді на повторювані/спам-повідомлення. Спробуйте ще раз трохи пізніше.",
        "banned_msg": "Доступ тимчасово обмежено. Якщо це помилка — напишіть менеджеру: maisondecafe.coffee@gmail.com",
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
        "spam_warn_1": "Похоже, это не вопрос 🙂 Я помогу, если вы уточните запрос про Maison de Café.",
        "spam_warn_2": "Я могу отвечать только на осмысленные вопросы, связанные с Maison de Café. Напишите, пожалуйста, что именно вас интересует.",
        "cooldown_msg": "Я временно перестану отвечать на повторяющиеся/спам-сообщения. Попробуйте чуть позже.",
        "banned_msg": "Доступ временно ограничен. Если это ошибка — напишите менеджеру: maisondecafe.coffee@gmail.com",
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
        "spam_warn_1": "This doesn't look like a real question 🙂 Please ask something specific about Maison de Café.",
        "spam_warn_2": "I can only answer meaningful questions related to Maison de Café. Please tell me what you need.",
        "cooldown_msg": "I’m temporarily pausing replies to repeated/spam messages. Please try again later.",
        "banned_msg": "Access is temporarily limited. If this is a mistake, contact: maisondecafe.coffee@gmail.com",
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
        "spam_warn_1": "Cela ne ressemble pas à une vraie question 🙂 Posez une question précise sur Maison de Café.",
        "spam_warn_2": "Je réponds uniquement aux questions pertinentes sur Maison de Café. Dites-moi ce dont vous avez besoin.",
        "cooldown_msg": "Je suspends temporairement les réponses aux messages répétitifs/spam. Réessayez plus tard.",
        "banned_msg": "Accès temporairement limité. Si c’est une erreur : maisondecafe.coffee@gmail.com",
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
        "spam_warn_1": "Dit lijkt geen echte vraag 🙂 Stel een concrete vraag over Maison de Café.",
        "spam_warn_2": "Ik kan alleen zinvolle vragen over Maison de Café beantwoorden. Wat wil je precies weten?",
        "cooldown_msg": "Ik pauzeer tijdelijk reacties op herhaalde/spam-berichten. Probeer later opnieuw.",
        "banned_msg": "Toegang tijdelijk beperkt. Als dit een vergissing is: maisondecafe.coffee@gmail.com",
        "contacts_text": (
            "Contact opnemen met Maison de Café kan via:\n\n"
            "• E-mail: maisondecafe.coffee@gmail.com\n"
            "• Telefoon: +32 470 600 806\n"
            "• Telegram-kanaal: https://t.me/maisondecafe\n\n"
            "Wil je — klik “Aanvraag achterlaten”, dan nemen we binnen 24 uur contact op."
        ),
    },
}

# Language behavior (base)
ASSISTANT_LANG_INSTRUCTIONS = {
    "ua": "Відповідай українською мовою. Якщо користувач пише іншою мовою — все одно відповідай українською.",
    "ru": "Отвечай на русском языке.",
    "en": "Respond in English.",
    "fr": "Réponds en français.",
    "nl": "Antwoord in het Nederlands.",
}

# =========================
# KB-ONLY + HUMAN STYLE (core)
# =========================
def build_core_instructions(lang: str, mode: str) -> str:
    """
    mode:
      - KB_ONLY: strictly from knowledge base
      - LEAD_MODE: short, guiding user to leave contacts + clarify
    """
    lang_instr = ASSISTANT_LANG_INSTRUCTIONS.get(lang, ASSISTANT_LANG_INSTRUCTIONS["ua"])

    kb_rules = (
        "ВАЖЛИВО: Відповідай ТІЛЬКИ на основі бази знань Maison de Café, яка прикріплена до цього ассистента. "
        "НЕ вигадуй і НЕ припускай. НЕ використовуй зовнішні джерела. "
        "Якщо точної відповіді немає у базі знань — чесно скажи, що у базі цього немає, "
        "і запропонуй залишити заявку (кнопка «Залишити заявку / Leave a request»), щоб менеджер відповів персонально."
    )

    human_style = (
        "Тон: максимально людяний, дружній, але професійний. "
        "Без роботських фраз типу «як ШІ…». "
        "Структура відповіді: коротко 1-2 речення по суті, далі 3-7 пунктів (•), наприкінці 1 CTA-рядок."
    )

    if mode == "LEAD_MODE":
        lead_style = (
            "Режим LEAD: відповідай дуже коротко і веди користувача до залишення контактів. "
            "Якщо користувач питає щось складне — коротко поясни по базі знань і одразу запропонуй залишити заявку."
        )
        return f"{lang_instr}\n\n{kb_rules}\n\n{human_style}\n\n{lead_style}"

    return f"{lang_instr}\n\n{kb_rules}\n\n{human_style}"


# =========================
# HELPERS
# =========================
def get_lang(user_id: str) -> str:
    return user_lang.get(user_id, "ua")  # default Ukrainian

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

def now_utc() -> datetime:
    # keep simple; Render logs use UTC typically. Not critical.
    return datetime.utcnow()

def is_owner(user_id: str) -> bool:
    return bool(OWNER_TELEGRAM_ID) and str(user_id) == str(OWNER_TELEGRAM_ID)

def normalize_text_for_spam(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text

def user_in_cooldown(user_id: str) -> bool:
    until = user_cooldown_until.get(user_id)
    if not until:
        return False
    if now_utc() >= until:
        user_cooldown_until.pop(user_id, None)
        return False
    return True

def user_is_banned(user_id: str) -> bool:
    until = banned_users.get(user_id)
    if not until:
        return False
    if until == datetime.max:
        return True
    if now_utc() < until:
        return True
    banned_users.pop(user_id, None)
    return False

def mark_debug(user_id: str, msg: str) -> None:
    user_last_debug[user_id] = msg


# =========================
# ANTI-SPAM
# =========================
SPAM_WINDOW_SECONDS = 12
SPAM_MAX_MSGS_IN_WINDOW = 6  # >6 messages in 12 sec => cooldown
SPAM_COOLDOWN_SECONDS = 60   # cooldown after rate-limit

REPEAT_SAME_MSG_THRESHOLD = 3  # same normalized message 3 times => cooldown
SPAM_STRIKES_TO_COOLDOWN = 2   # after 2 strikes => cooldown
SPAM_STRIKE_COOLDOWN_SECONDS = 120

def looks_like_gibberish(text: str) -> bool:
    """
    Detect patterns like: "оооооо", "ла-ла-ла", "....", random repeats, etc.
    This is conservative to reduce false positives.
    """
    if not text:
        return True

    raw = text.strip()
    if len(raw) <= 2:
        return True

    # many repeated same character (e.g. ooooooo, .......)
    if re.fullmatch(r"(.)\1{6,}", raw, flags=re.DOTALL):
        return True

    # repeated syllables/words (e.g. "ла ла ла ла", "тра-ля-ля", "ооо ооо")
    simplified = re.sub(r"[^a-zA-Zа-яА-ЯіїєґІЇЄҐ0-9]+", " ", raw.lower()).strip()
    if simplified:
        parts = simplified.split()
        if len(parts) >= 4 and len(set(parts)) == 1:
            return True

    # mostly punctuation
    letters_digits = sum(ch.isalnum() for ch in raw)
    if letters_digits <= max(2, int(len(raw) * 0.15)):
        return True

    # excessive repeated bigrams like "lolololol", "ooooaa"
    if re.search(r"(..)\1{4,}", raw.lower()):
        return True

    return False

def anti_spam_check(user_id: str, text: str) -> Tuple[bool, Optional[str]]:
    """
    Returns (should_ignore, optional_reply_to_user).
    should_ignore True => do not call OpenAI (no token burn).
    """
    # banned check first
    if user_is_banned(user_id):
        return True, t(user_id, "banned_msg")

    # cooldown check
    if user_in_cooldown(user_id):
        return True, None

    ntext = normalize_text_for_spam(text)

    # rate limit
    now = now_utc()
    times = user_msg_times.get(user_id, [])
    times = [ts for ts in times if (now - ts).total_seconds() <= SPAM_WINDOW_SECONDS]
    times.append(now)
    user_msg_times[user_id] = times

    if len(times) > SPAM_MAX_MSGS_IN_WINDOW:
        user_cooldown_until[user_id] = now + timedelta(seconds=SPAM_COOLDOWN_SECONDS)
        return True, t(user_id, "cooldown_msg")

    # repeat check
    last = user_last_text.get(user_id, "")
    if ntext and ntext == last:
        user_repeat_count[user_id] = user_repeat_count.get(user_id, 0) + 1
    else:
        user_repeat_count[user_id] = 0
        user_last_text[user_id] = ntext

    if user_repeat_count.get(user_id, 0) >= REPEAT_SAME_MSG_THRESHOLD:
        user_cooldown_until[user_id] = now + timedelta(seconds=SPAM_STRIKE_COOLDOWN_SECONDS)
        return True, t(user_id, "cooldown_msg")

    # gibberish check
    if looks_like_gibberish(text):
        strikes = user_spam_strikes.get(user_id, 0) + 1
        user_spam_strikes[user_id] = strikes

        if strikes == 1:
            return True, t(user_id, "spam_warn_1")
        if strikes == 2:
            return True, t(user_id, "spam_warn_2")

        # cooldown after repeated strikes
        if strikes >= SPAM_STRIKES_TO_COOLDOWN:
            user_cooldown_until[user_id] = now + timedelta(seconds=SPAM_STRIKE_COOLDOWN_SECONDS)
            return True, t(user_id, "cooldown_msg")

    # looks ok
    return False, None


# =========================
# BUTTON -> COMMAND PROMPT MAPPING
# =========================
def button_to_prompt(lang: str, pressed_text: str) -> Optional[str]:
    """
    Convert menu button presses into strong, non-ambiguous prompts
    so the assistant answers about Maison de Café (not generic).
    """
    m = MENU[lang]

    if pressed_text == m["what"]:
        return (
            "Поясни, що таке Maison de Café: що саме купує клієнт, що входить у рішення під ключ, "
            "як працює кав’ярня самообслуговування. Дай відповідь структуровано і додай CTA (залишити заявку)."
        )
    if pressed_text == m["price"]:
        return (
            "Поясни вартість відкриття кав’ярні самообслуговування Maison de Café. "
            "Що входить у базову вартість, які є регулярні платежі (термінал/послуги), "
            "як оплачується (60% аванс / 40% при передачі). Структуровано + CTA."
        )
    if pressed_text == m["payback"]:
        return (
            "Поясни окупність та прибуток: базова модель (35 чашок/день), середня маржа, "
            "що впливає на окупність, які ризики та що робити якщо локація слабка. Структуровано + CTA."
        )
    if pressed_text == m["franchise"]:
        return (
            "Поясни умови співпраці Maison de Café: формат договору послуг, підтримка, інгредієнти, "
            "обов’язки сторін, гарантія, релокація. Структуровано + CTA."
        )
    # contacts and lead handled elsewhere
    return None


# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id not in user_lang:
        user_lang[user_id] = "ua"

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
def sanitize_phone(s: str) -> str:
    return re.sub(r"[^\d\+\-\s\(\)]", "", (s or "").strip())

def is_valid_email(s: str) -> bool:
    s = (s or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s))

async def start_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lead_states[user_id] = "name"
    lead_data[user_id] = {}
    await update.message.reply_text(
        t(user_id, "lead_start"),
        reply_markup=mk_main_keyboard(get_lang(user_id)),
    )

async def handle_lead_form_text(user_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    lang = get_lang(user_id)
    step = lead_states.get(user_id)
    text = (text or "").strip()

    if not step:
        return False

    if step == "name":
        if len(text) < 2:
            await update.message.reply_text(TEXTS[lang]["lead_start"], reply_markup=mk_main_keyboard(lang))
            return True
        lead_data[user_id]["name"] = text
        lead_states[user_id] = "phone"
        await update.message.reply_text(TEXTS[lang]["lead_phone"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "phone":
        phone = sanitize_phone(text)
        if len(re.sub(r"\D", "", phone)) < 7:
            await update.message.reply_text(TEXTS[lang]["lead_phone"], reply_markup=mk_main_keyboard(lang))
            return True
        lead_data[user_id]["phone"] = phone
        lead_states[user_id] = "email"
        await update.message.reply_text(TEXTS[lang]["lead_email"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "email":
        if not is_valid_email(text):
            await update.message.reply_text(TEXTS[lang]["lead_email"], reply_markup=mk_main_keyboard(lang))
            return True
        lead_data[user_id]["email"] = text
        lead_states[user_id] = "message"
        await update.message.reply_text(TEXTS[lang]["lead_msg"], reply_markup=mk_main_keyboard(lang))
        return True

    if step == "message":
        if len(text) < 3:
            await update.message.reply_text(TEXTS[lang]["lead_msg"], reply_markup=mk_main_keyboard(lang))
            return True

        lead_data[user_id]["message"] = text
        lead_states.pop(user_id, None)

        username = update.effective_user.username or ""
        now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        payload = (
            f"Maison de Café — NEW LEAD\n"
            f"Telegram user_id: {user_id}\n"
            f"Username: @{username}\n"
            f"Name: {lead_data[user_id].get('name','')}\n"
            f"Phone: {lead_data[user_id].get('phone','')}\n"
            f"Email: {lead_data[user_id].get('email','')}\n"
            f"Message: {lead_data[user_id].get('message','')}\n"
            f"Time: {now_local}\n"
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
            email_note = (
                "Примітка: відправка на email не налаштована (SMTP). "
                "Сповіщення власнику відправлено в Telegram."
                if owner_notified else
                "Примітка: email (SMTP) не налаштовано, і Telegram-сповіщення власнику не відправлено."
            )

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
async def ask_assistant(user_id: str, user_text: str, mode: str = "KB_ONLY") -> str:
    thread_id = ensure_thread(user_id)
    lang = get_lang(user_id)

    # message
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_text,
    )

    # run with strong instructions
    instructions = build_core_instructions(lang=lang, mode=mode)

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        instructions=instructions,
    )

    # wait completion with timeout
    start_ts = now_utc()
    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == "completed":
            break
        if run_status.status in ["failed", "cancelled", "expired"]:
            return ""
        if (now_utc() - start_ts).total_seconds() > 60:
            return ""
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return ""

    # newest assistant message usually at index 0, but safe-check content
    try:
        return messages.data[0].content[0].text.value
    except Exception:
        return ""


# =========================
# ADMIN COMMANDS
# =========================
async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    total_threads = len(user_threads)
    total_lang = len(user_lang)
    total_leads_in_progress = len(lead_states)
    total_banned = len([u for u in banned_users.keys() if user_is_banned(u)])

    msg = (
        "📊 Bot status\n"
        f"Threads: {total_threads}\n"
        f"Users with lang: {total_lang}\n"
        f"Lead forms in progress: {total_leads_in_progress}\n"
        f"Banned users: {total_banned}\n"
    )
    await update.message.reply_text(msg)

async def admin_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    # If passed a user id: /debug 12345
    target = None
    if context.args:
        target = str(context.args[0]).strip()
    else:
        target = user_id

    dbg = user_last_debug.get(target, "(no debug info)")
    await update.message.reply_text(f"🧩 Debug for {target}:\n{dbg}")

async def admin_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setlang <user_id> <ua|ru|en|fr|nl>")
        return

    target = str(context.args[0]).strip()
    lang = str(context.args[1]).strip().lower()
    if lang not in LANGS:
        await update.message.reply_text("Invalid lang. Use: ua|ru|en|fr|nl")
        return

    user_lang[target] = lang
    await update.message.reply_text(f"✅ Set language for {target} => {lang}")

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id> [minutes|perm]")
        return

    target = str(context.args[0]).strip()
    dur = "perm"
    if len(context.args) >= 2:
        dur = str(context.args[1]).strip().lower()

    if dur == "perm":
        banned_users[target] = datetime.max
        await update.message.reply_text(f"⛔ Permanently banned {target}")
        return

    try:
        mins = int(dur)
        banned_users[target] = now_utc() + timedelta(minutes=mins)
        await update.message.reply_text(f"⛔ Banned {target} for {mins} minutes")
    except Exception:
        await update.message.reply_text("Invalid duration. Use minutes number or 'perm'.")

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    target = str(context.args[0]).strip()
    banned_users.pop(target, None)
    await update.message.reply_text(f"✅ Unbanned {target}")

async def admin_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return

    msg = (
        "⚙️ Limits\n"
        f"Rate window: {SPAM_WINDOW_SECONDS}s\n"
        f"Max msgs in window: {SPAM_MAX_MSGS_IN_WINDOW}\n"
        f"Cooldown (rate): {SPAM_COOLDOWN_SECONDS}s\n"
        f"Repeat threshold: {REPEAT_SAME_MSG_THRESHOLD}\n"
        f"Cooldown (strikes): {SPAM_STRIKE_COOLDOWN_SECONDS}s\n"
    )
    await update.message.reply_text(msg)


# =========================
# CORE ROUTING (text/voice)
# =========================
async def route_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, text: str):
    """
    Single routing function for both TEXT and VOICE transcripts.
    """
    lang = get_lang(user_id)
    text = (text or "").strip()

    mark_debug(user_id, f"route_user_text: lang={lang}, in_lead={user_id in lead_states}, text='{text[:120]}'")

    # Anti-spam check FIRST (avoid OpenAI burn)
    ignore, reply = anti_spam_check(user_id, text)
    if ignore:
        if reply:
            await update.message.reply_text(reply, reply_markup=mk_main_keyboard(lang))
        return

    # Lead form step processing priority
    if user_id in lead_states:
        handled = await handle_lead_form_text(user_id, update, context, text)
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

    # Contacts (static) -> Lead-mode for short guidance
    if is_contacts_button(text):
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    # If pressed a menu button like "What/Price/Payback/Franchise"
    prompt = button_to_prompt(lang, text)
    if prompt:
        # Treat as KB_ONLY but with explicit topic
        try:
            ai_reply = await ask_assistant(user_id, prompt, mode="KB_ONLY")
            if not ai_reply:
                await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
                return
            await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))
            return
        except Exception as e:
            print("ASSISTANT ERROR:", repr(e))
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return

    # Otherwise free-text -> KB_ONLY
    try:
        ai_reply = await ask_assistant(user_id, text, mode="KB_ONLY")
        if not ai_reply:
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return
        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(lang))
    except Exception as e:
        print("ASSISTANT ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))


# =========================
# TEXT HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # default UA
    if user_id not in user_lang:
        user_lang[user_id] = "ua"

    # banned?
    if user_is_banned(user_id):
        await update.message.reply_text(t(user_id, "banned_msg"), reply_markup=mk_main_keyboard(get_lang(user_id)))
        return

    text = (update.message.text or "").strip()
    await route_user_text(update, context, user_id, text)


# =========================
# VOICE HANDLER
# =========================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id not in user_lang:
        user_lang[user_id] = "ua"

    lang = get_lang(user_id)

    # banned?
    if user_is_banned(user_id):
        await update.message.reply_text(TEXTS[lang]["banned_msg"], reply_markup=mk_main_keyboard(lang))
        return

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
        user_text = (getattr(transcript, "text", "") or "").strip()

        if not user_text:
            await update.message.reply_text(TEXTS[lang]["voice_fail"], reply_markup=mk_main_keyboard(lang))
            return

        mark_debug(user_id, f"voice_transcript: '{user_text[:200]}'")

        # Route transcript through the same pipeline (anti-spam included)
        await route_user_text(update, context, user_id, user_text)

    except Exception as e:
        print("VOICE ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))


# =========================
# ENTRYPOINT
# =========================
def main():
    print("🚀 Bot is starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # user
    application.add_handler(CommandHandler("start", start))

    # admin
    application.add_handler(CommandHandler("status", admin_status))
    application.add_handler(CommandHandler("debug", admin_debug))
    application.add_handler(CommandHandler("setlang", admin_setlang))
    application.add_handler(CommandHandler("ban", admin_ban))
    application.add_handler(CommandHandler("unban", admin_unban))
    application.add_handler(CommandHandler("limits", admin_limits))

    # voice before text
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # IMPORTANT:
    # drop_pending_updates helps avoid old queued updates after restarts
    # (but it does NOT solve Conflict if two instances are running).
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
