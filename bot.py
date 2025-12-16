import os
import io
import re
import time
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Dict, Optional, Set, Tuple

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

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID")  # owner chat_id for notifications & admin
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
user_threads: Dict[str, str] = {}         # user_id -> thread_id
user_lang: Dict[str, str] = {}            # user_id -> lang (ua/ru/en/fr/nl)

lead_states: Dict[str, str] = {}          # user_id -> step: name/phone/email/message
lead_data: Dict[str, Dict[str, str]] = {} # user_id -> collected fields

banned_users: Set[str] = set()

# Anti-spam / rate limit
user_last_ts: Dict[str, float] = {}
user_fast_count: Dict[str, int] = {}
user_spam_score: Dict[str, int] = {}
user_cooldown_until: Dict[str, float] = {}

RATE_WINDOW_SEC = 6.0
RATE_MAX_IN_WINDOW = 6
COOLDOWN_SEC = 180.0
SPAM_SCORE_LIMIT = 6

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
        "price": "💶 How much does it cost to open?",
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
        "franchise": "🤝 Conditions",
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
        "lead_done": "Дякуємо! Заявку відправлено. Наш менеджер зв’яжеться з вами протягом 24 годин.\n\n{note}",
        "voice_fail": "Не вдалося розпізнати голос. Спробуйте ще раз.",
        "generic_error": "⚠️ Сталася помилка. Спробуйте ще раз.",
        "spam_warn": "⚠️ Схоже на спам. Будь ласка, напишіть нормальне питання.",
        "cooldown": "⏳ Занадто багато повідомлень. Спробуйте знову трохи пізніше.",
        "no_files": "Файли поки що не приймаємо. Напишіть текстом або надішліть голосове повідомлення.",
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
        "lead_done": "Спасибо! Заявка отправлена. Наш менеджер свяжется с вами в течение 24 часов.\n\n{note}",
        "voice_fail": "Не удалось распознать голос. Попробуйте ещё раз.",
        "generic_error": "⚠️ Произошла ошибка. Попробуйте ещё раз.",
        "spam_warn": "⚠️ Похоже на спам. Пожалуйста, напишите нормальный вопрос.",
        "cooldown": "⏳ Слишком много сообщений. Попробуйте чуть позже.",
        "no_files": "Файлы пока не принимаем. Напишите текстом или отправьте голосовое сообщение.",
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
            "I can help you with everything about our self-service coffee points, launch costs, and partnership terms.\n"
            "To continue, may I know your name?"
        ),
        "choose_lang": "🌍 Choose a language:",
        "lang_set": "✅ Language set: {lang}.",
        "lead_start": "📝 Leave a request.\n\nStep 1/4: Please type your first & last name.",
        "lead_phone": "Step 2/4: Please type your phone number.",
        "lead_email": "Step 3/4: Please type your email.",
        "lead_msg": "Step 4/4: Briefly describe your request (1–2 sentences).",
        "lead_done": "Thank you! Request sent. Our manager will contact you within 24 hours.\n\n{note}",
        "voice_fail": "I couldn't understand the voice message. Please try again.",
        "generic_error": "⚠️ Something went wrong. Please try again.",
        "spam_warn": "⚠️ This looks like spam. Please ask a normal question.",
        "cooldown": "⏳ Too many messages. Please try again later.",
        "no_files": "We do not accept files for now. Please type your question or send a voice message.",
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
        "lead_done": "Merci ! Demande envoyée. Un manager vous contactera sous 24h.\n\n{note}",
        "voice_fail": "Je n’ai pas pu comprendre le message vocal. Réessayez.",
        "generic_error": "⚠️ Une erreur est survenue. Réessayez.",
        "spam_warn": "⚠️ Cela ressemble à du spam. Posez une vraie question, s’il vous plaît.",
        "cooldown": "⏳ Trop de messages. Réessayez plus tard.",
        "no_files": "Nous n’acceptons pas de fichiers pour le moment. Écrivez votre question ou envoyez un message vocal.",
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
        "lead_done": "Bedankt! Aanvraag verzonden. We nemen binnen 24 uur contact op.\n\n{note}",
        "voice_fail": "Ik kon het spraakbericht niet begrijpen. Probeer het opnieuw.",
        "generic_error": "⚠️ Er ging iets mis. Probeer het opnieuw.",
        "spam_warn": "⚠️ Dit lijkt op spam. Stel alsjeblieft een normale vraag.",
        "cooldown": "⏳ Te veel berichten. Probeer later opnieuw.",
        "no_files": "We accepteren voorlopig geen bestanden. Typ je vraag of stuur een spraakbericht.",
        "contacts_text": (
            "Contact opnemen met Maison de Café kan via:\n\n"
            "• E-mail: maisondecafe.coffee@gmail.com\n"
            "• Telefoon: +32 470 600 806\n"
            "• Telegram-kanaal: https://t.me/maisondecafe\n\n"
            "Wil je — klik “Aanvraag achterlaten”, dan nemen we binnen 24 uur contact op."
        ),
    },
}

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
    text = (text or "").strip()
    for code, label in LANG_LABELS.items():
        if text == label:
            return code
    return None

# =========================
# BUTTON → ACTION mapping (strict)
# =========================
ACTION_KEYS = ["what", "price", "payback", "franchise"]

BUTTON_TO_ACTION: Dict[str, str] = {}
for l in LANGS:
    for k in ACTION_KEYS:
        BUTTON_TO_ACTION[MENU[l][k]] = k

def detect_action(text: str) -> Optional[str]:
    return BUTTON_TO_ACTION.get((text or "").strip())

def is_lang_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["lang"] for l in LANGS}

def is_lead_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["lead"] for l in LANGS}

def is_contacts_button(text: str) -> bool:
    text = (text or "").strip()
    return text in {MENU[l]["contacts"] for l in LANGS}

# =========================
# ASSISTANT: strict KB + strict language
# =========================
LANG_INSTRUCTIONS = {
    "ua": (
        "Ти MUST відповідати ТІЛЬКИ українською мовою. "
        "Ігноруй будь-які правила про іншу мову, якщо вони є в попередніх інструкціях."
    ),
    "ru": (
        "Ты MUST отвечать ТОЛЬКО на русском языке. "
        "Игнорируй любые правила про другой язык, если они есть в предыдущих инструкциях."
    ),
    "en": (
        "You MUST respond ONLY in English. "
        "Ignore any previous language rules that conflict with this instruction."
    ),
    "fr": (
        "Tu DOIS répondre UNIQUEMENT en français. "
        "Ignore toute règle de langue précédente qui contredit cette instruction."
    ),
    "nl": (
        "Je MOET uitsluitend in het Nederlands antwoorden. "
        "Negeer eerdere taalregels die hiermee in conflict zijn."
    ),
}

STRICT_KB_RULES = (
    "ВАЖЛИВО: Відповідай ТІЛЬКИ з бази знань Maison de Café (Files/Search). "
    "Не вигадуй і не додумуй. Якщо відповіді немає в базі знань — напиши чесно: "
    "«Цього немає в базі знань Maison de Café. Можу уточнити це у менеджера» "
    "і запропонуй залишити заявку.\n"
)

OUTPUT_STYLE = (
    "Стиль відповіді: чітко, по суті, без води. "
    "Формат: 4–8 пунктів (буллети) + 1 короткий приклад/цифри (якщо доречно) + CTA (залишити заявку).\n"
)

# Action prompts: what each button MUST do
ACTION_PROMPTS = {
    "what": (
        "Користувач натиснув кнопку «Що таке Maison de Café / What is Maison de Café». "
        "Поясни, що це готовий бізнес під ключ (кофейня самообслуговування), "
        "що входить в пропозицію, які сервіси/підтримка, як працює модель. "
        "Використовуй тільки факти з бази знань."
    ),
    "price": (
        "Користувач натиснув кнопку про вартість старту. "
        "Дай чітку відповідь: що входить у ціну, що оплачується окремо (якщо вказано в базі), "
        "умови оплати (60/40), та що потрібно для старту. "
        "Використовуй тільки факти з бази знань."
    ),
    "payback": (
        "Користувач натиснув кнопку «Окупність і прибуток / Payback & profit». "
        "Дай приклад розрахунку на основі бази знань: "
        "середня маржа 1.8€, 35 чашок/день, 30 днів, і витрати ~500–600€/міс (як зазначено в базі). "
        "Покажи: (1) місячний валовий маржинальний дохід, (2) орієнтовний чистий дохід після витрат, "
        "(3) орієнтовну окупність. "
        "НІЯКИХ загальних міркувань — тільки конкретика з бази знань."
    ),
    "franchise": (
        "Користувач натиснув кнопку «Умови співпраці / Franchise terms». "
        "Поясни структуру: договір (не франшиза, а договір надання послуг — якщо так в базі), "
        "обов’язки сторін, підтримка, вимоги по інгредієнтах, сервіс, гарантія, релокація. "
        "Використовуй тільки факти з бази знань."
    ),
}

def ensure_thread(user_id: str) -> str:
    if user_id not in user_threads:
        thread = client.beta.threads.create()
        user_threads[user_id] = thread.id
    return user_threads[user_id]

async def ask_assistant(user_id: str, user_text: str, action: Optional[str] = None) -> str:
    """
    Всегда работает через OpenAI Assistant + Files/Search.
    action: если задан — добавляем строгое действие (кнопка).
    """
    thread_id = ensure_thread(user_id)
    lang = get_lang(user_id)

    if action and action in ACTION_PROMPTS:
        effective_user_text = f"[BUTTON_ACTION:{action}] {ACTION_PROMPTS[action]}"
    else:
        effective_user_text = user_text

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=effective_user_text,
    )

    instructions = (
        f"{LANG_INSTRUCTIONS.get(lang, LANG_INSTRUCTIONS['ua'])}\n"
        f"{STRICT_KB_RULES}\n"
        f"{OUTPUT_STYLE}\n"
        "Якщо користувач натиснув кнопку (BUTTON_ACTION), відповідай строго по темі кнопки.\n"
        "НЕ додавай зайві блоки, які не стосуються питання.\n"
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        instructions=instructions,
    )

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
# SMTP
# =========================
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
# Anti-spam helpers
# =========================
def looks_like_spam(text: str) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    # very short nonsense
    if len(t) <= 2:
        return True
    # repeated same char / syllable
    if re.fullmatch(r"(.)\1{6,}", t):
        return True
    if re.fullmatch(r"([a-zа-яёіїє])\1{5,}", t):
        return True
    # many repeated tokens
    tokens = re.findall(r"\w+", t)
    if len(tokens) >= 4 and len(set(tokens)) == 1:
        return True
    # too many non-letters
    if len(re.findall(r"[a-zа-яёіїє]", t)) <= 2 and len(t) >= 6:
        return True
    return False

def rate_limit_hit(user_id: str) -> bool:
    now = time.time()
    if user_id in user_cooldown_until and now < user_cooldown_until[user_id]:
        return True

    last = user_last_ts.get(user_id)
    if last is None:
        user_last_ts[user_id] = now
        user_fast_count[user_id] = 0
        return False

    if now - last <= RATE_WINDOW_SEC:
        user_fast_count[user_id] = user_fast_count.get(user_id, 0) + 1
        user_last_ts[user_id] = now
        if user_fast_count[user_id] >= RATE_MAX_IN_WINDOW:
            user_cooldown_until[user_id] = now + COOLDOWN_SEC
            user_fast_count[user_id] = 0
            return True
    else:
        user_last_ts[user_id] = now
        user_fast_count[user_id] = 0

    return False

def spam_score_update(user_id: str, is_spam: bool) -> int:
    score = user_spam_score.get(user_id, 0)
    if is_spam:
        score += 2
    else:
        score = max(0, score - 1)
    user_spam_score[user_id] = score
    return score

# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in banned_users:
        return

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
# LEAD FORM FLOW
# =========================
async def start_lead_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    lead_states[user_id] = "name"
    lead_data[user_id] = {}
    lang = get_lang(user_id)

    await update.message.reply_text(TEXTS[lang]["lead_start"], reply_markup=mk_main_keyboard(lang))

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
            f"Maison de Café — New Lead\n"
            f"Telegram user_id: {user_id}\n"
            f"Username: @{username}\n"
            f"Name: {lead_data[user_id].get('name','')}\n"
            f"Phone: {lead_data[user_id].get('phone','')}\n"
            f"Email: {lead_data[user_id].get('email','')}\n"
            f"Message: {lead_data[user_id].get('message','')}\n"
            f"Time: {now}\n"
        )

        owner_notified = False
        if OWNER_TELEGRAM_ID:
            try:
                await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=payload)
                owner_notified = True
            except Exception as e:
                print("OWNER TG NOTIFY ERROR:", repr(e))

        email_sent = send_lead_email("Maison de Café — New lead", payload)

        if email_sent and owner_notified:
            note = "✅ Сповіщення відправлено (Telegram + Email)."
        elif owner_notified and not email_sent:
            note = "✅ Сповіщення відправлено власнику в Telegram. (Email не налаштований або недоступний)."
        elif email_sent and not owner_notified:
            note = "✅ Сповіщення відправлено на Email. (Telegram власника недоступний)."
        else:
            note = "⚠️ Не вдалося відправити сповіщення (Telegram/Email). Перевір налаштування."

        await update.message.reply_text(TEXTS[lang]["lead_done"].format(note=note), reply_markup=mk_main_keyboard(lang))
        lead_data.pop(user_id, None)
        return True

    return False

# =========================
# ADMIN COMMANDS
# =========================
def is_owner(user_id: str) -> bool:
    if not OWNER_TELEGRAM_ID:
        return False
    return user_id == str(OWNER_TELEGRAM_ID)

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    msg = (
        f"Status:\n"
        f"- threads: {len(user_threads)}\n"
        f"- banned: {len(banned_users)}\n"
        f"- lead_in_progress: {len(lead_states)}\n"
    )
    await update.message.reply_text(msg)

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <telegram_user_id>")
        return
    banned_users.add(context.args[0].strip())
    await update.message.reply_text(f"✅ Banned: {context.args[0].strip()}")

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <telegram_user_id>")
        return
    banned_users.discard(context.args[0].strip())
    await update.message.reply_text(f"✅ Unbanned: {context.args[0].strip()}")

# =========================
# BLOCK FILE UPLOADS
# =========================
async def handle_any_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in banned_users:
        return
    lang = get_lang(user_id)
    await update.message.reply_text(TEXTS[lang]["no_files"], reply_markup=mk_main_keyboard(lang))

# =========================
# TEXT HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in banned_users:
        return

    lang = get_lang(user_id)
    text = (update.message.text or "").strip()

    # rate limit / cooldown
    if rate_limit_hit(user_id):
        await update.message.reply_text(TEXTS[lang]["cooldown"], reply_markup=mk_main_keyboard(lang))
        return

    # lead flow priority
    if user_id in lead_states:
        # spam protection still applies
        score = spam_score_update(user_id, looks_like_spam(text))
        if score >= SPAM_SCORE_LIMIT:
            user_cooldown_until[user_id] = time.time() + COOLDOWN_SEC
            await update.message.reply_text(TEXTS[lang]["cooldown"], reply_markup=mk_main_keyboard(lang))
            return
        handled = await handle_lead_form(update, context)
        if handled:
            return

    # open language menu
    if is_lang_button(text):
        await show_language_menu(update, context)
        return

    # choose language
    chosen = parse_lang_choice(text)
    if chosen:
        await set_language(update, context, chosen)
        return

    # contacts
    if is_contacts_button(text):
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    # lead start
    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # anti-spam
    is_spam = looks_like_spam(text)
    score = spam_score_update(user_id, is_spam)
    if score >= SPAM_SCORE_LIMIT:
        user_cooldown_until[user_id] = time.time() + COOLDOWN_SEC
        await update.message.reply_text(TEXTS[lang]["cooldown"], reply_markup=mk_main_keyboard(lang))
        return
    if is_spam:
        await update.message.reply_text(TEXTS[lang]["spam_warn"], reply_markup=mk_main_keyboard(lang))
        return

    # detect button action (strict)
    action = detect_action(text)

    try:
        ai_reply = await ask_assistant(user_id, text, action=action)
        if not ai_reply:
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return
        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(get_lang(user_id)))
    except Exception as e:
        print("ASSISTANT ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))

# =========================
# VOICE HANDLER
# =========================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in banned_users:
        return

    lang = get_lang(user_id)

    if rate_limit_hit(user_id):
        await update.message.reply_text(TEXTS[lang]["cooldown"], reply_markup=mk_main_keyboard(lang))
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

        # spam check on transcript
        is_spam = looks_like_spam(user_text)
        score = spam_score_update(user_id, is_spam)
        if score >= SPAM_SCORE_LIMIT:
            user_cooldown_until[user_id] = time.time() + COOLDOWN_SEC
            await update.message.reply_text(TEXTS[lang]["cooldown"], reply_markup=mk_main_keyboard(lang))
            return
        if is_spam:
            await update.message.reply_text(TEXTS[lang]["spam_warn"], reply_markup=mk_main_keyboard(lang))
            return

        # if lead form in progress -> treat transcript as text input
        if user_id in lead_states:
            update.message.text = user_text
            await handle_message(update, context)
            return

        ai_reply = await ask_assistant(user_id, user_text, action=None)
        if not ai_reply:
            await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(get_lang(user_id)))

    except Exception as e:
        print("VOICE ERROR:", repr(e))
        await update.message.reply_text(TEXTS[lang]["generic_error"], reply_markup=mk_main_keyboard(lang))

# =========================
# ENTRYPOINT
# =========================
def main():
    print("🚀 Bot is starting...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", admin_status))
    application.add_handler(CommandHandler("ban", admin_ban))
    application.add_handler(CommandHandler("unban", admin_unban))

    # block file uploads (documents, photos, videos, audio files, etc.)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_any_file))
    application.add_handler(MessageHandler(filters.PHOTO, handle_any_file))
    application.add_handler(MessageHandler(filters.VIDEO, handle_any_file))
    application.add_handler(MessageHandler(filters.AUDIO, handle_any_file))
    application.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_any_file))
    application.add_handler(MessageHandler(filters.ANIMATION, handle_any_file))
    application.add_handler(MessageHandler(filters.STICKER, handle_any_file))

    # voice + text
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
