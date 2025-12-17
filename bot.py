import os
import io
import re
import time
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Dict, Optional, Tuple, Set, List

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
# (user_id, lang) -> thread_id
user_threads: Dict[Tuple[str, str], str] = {}

# user_id -> selected lang (ua/ru/en/fr/nl)
user_lang: Dict[str, str] = {}

# Lead form state
lead_states: Dict[str, str] = {}                # user_id -> step
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
            "Я допоможу вам розібратися у питаннях про наші кав’ярні самообслуговування, запуск і умови співпраці.\n"
            "Оберіть, будь ласка, кнопку з меню або задайте питання."
        ),
        "choose_lang": "🌍 Оберіть мову:",
        "lang_set": "✅ Мову змінено: {lang}.",
        "lead_start": "📝 Залишити заявку.\n\nКрок 1/4: Напишіть ваше ім’я та прізвище.",
        "lead_phone": "Крок 2/4: Напишіть ваш номер телефону.",
        "lead_email": "Крок 3/4: Напишіть ваш email.",
        "lead_msg": "Крок 4/4: Коротко опишіть ваш запит (1–2 речення).",
        "lead_done": (
            "Дякуємо! Заявку відправлено. Менеджер зв’яжеться з вами протягом 24 годин.\n\n{email_note}"
        ),
        "voice_fail": "Не вдалося розпізнати голос. Спробуйте ще раз.",
        "generic_error": "⚠️ Сталася помилка. Спробуйте ще раз.",
        "kb_missing": (
            "Я не знайшов цього у базі знань Maison de Café.\n"
            "Щоб відповісти точно — залиште, будь ласка, заявку, і менеджер допоможе."
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
            "Помогу с вопросами про формат кофепоинтов самообслуживания, запуск и условия сотрудничества.\n"
            "Выберите кнопку меню или задайте вопрос."
        ),
        "choose_lang": "🌍 Выберите язык:",
        "lang_set": "✅ Язык установлен: {lang}.",
        "lead_start": "📝 Оставить заявку.\n\nШаг 1/4: Напишите ваше имя и фамилию.",
        "lead_phone": "Шаг 2/4: Напишите ваш номер телефона.",
        "lead_email": "Шаг 3/4: Напишите ваш email.",
        "lead_msg": "Шаг 4/4: Коротко опишите запрос (1–2 предложения).",
        "lead_done": (
            "Спасибо! Заявка отправлена. Менеджер свяжется с вами в течение 24 часов.\n\n{email_note}"
        ),
        "voice_fail": "Не удалось распознать голос. Попробуйте ещё раз.",
        "generic_error": "⚠️ Произошла ошибка. Попробуйте ещё раз.",
        "kb_missing": (
            "Я не нашёл этого в базе знаний Maison de Café.\n"
            "Чтобы ответить точно — оставьте, пожалуйста, заявку, и менеджер поможет."
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
            "I’m Max, Maison de Café virtual assistant.\n"
            "Ask a question or use the menu buttons below."
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
            "I couldn’t find this in the Maison de Café knowledge base.\n"
            "To answer accurately, please leave a request and a manager will help you."
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
            "Je suis Max, assistant virtuel Maison de Café.\n"
            "Posez une question ou utilisez le menu ci-dessous."
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
            "Je n’ai pas trouvé cela dans la base de connaissances Maison de Café.\n"
            "Pour répondre précisément, laissez une demande et un manager vous aidera."
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
            "Stel je vraag of gebruik het menu hieronder."
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
            "Ik kon dit niet vinden in de Maison de Café kennisbank.\n"
            "Voor een exact antwoord: laat een aanvraag achter en een manager helpt je."
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
BUTTON_LOOKUP: Dict[str, Tuple[str, str]] = {}
for lang in LANGS:
    for key, label in MENU[lang].items():
        BUTTON_LOOKUP[label] = (key, lang)


# =========================
# HELPERS
# =========================
def get_lang(user_id: str) -> str:
    return user_lang.get(user_id, "ua")

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
# HUMAN CONSULTANT + STRICT KB (CORPORATE COMPLIANCE)
# =========================

HUMAN_CONSULTANT_RULES = {
    "ua": (
        "ROLE: Human Consultant (Sales + Compliance).\n"
        "TONE: людяний, коротко, структуровано, без води.\n"
        "SCOPE: ТІЛЬКИ Maison de Café (не 'звичайна кав’ярня', не сторонні моделі).\n"
        "COMPLIANCE: НЕ вигадувати, НЕ додумувати. Якщо факту нема в KB — kb_missing.\n"
        "MATH: якщо питання математичне і в ньому є числа/параметри — порахуй точно, "
        "але НЕ додавай припущень (тільки те, що дано або що є в KB).\n"
    ),
    "ru": (
        "ROLE: Human Consultant (Sales + Compliance).\n"
        "TONE: по-человечески, коротко, структурированно.\n"
        "SCOPE: ТОЛЬКО Maison de Café (не 'обычная кофейня', не сторонние модели).\n"
        "COMPLIANCE: НЕ выдумывать, НЕ додумывать. Если факта нет в KB — kb_missing.\n"
        "MATH: если вопрос математический и в нём есть числа/параметры — посчитай точно, "
        "но НЕ добавляй предположений (только дано или из KB).\n"
    ),
    "en": (
        "ROLE: Human Consultant (Sales + Compliance).\n"
        "TONE: human, concise, structured.\n"
        "SCOPE: ONLY Maison de Café (no generic coffee shop advice).\n"
        "COMPLIANCE: Do NOT invent or guess. If not in KB — kb_missing.\n"
        "MATH: if the question is mathematical and includes inputs — compute accurately without assumptions.\n"
    ),
    "fr": (
        "ROLE: Human Consultant (Sales + Compliance).\n"
        "TONE: humain, concis, structuré.\n"
        "SCOPE: UNIQUEMENT Maison de Café (pas de conseils génériques).\n"
        "COMPLIANCE: Ne pas inventer. Si absent de la KB — kb_missing.\n"
        "MATH: si question mathématique avec données — calcule précisément sans hypothèses.\n"
    ),
    "nl": (
        "ROLE: Human Consultant (Sales + Compliance).\n"
        "TONE: menselijk, kort, gestructureerd.\n"
        "SCOPE: ALLEEN Maison de Café (geen algemene koffiezaak-adviezen).\n"
        "COMPLIANCE: Niet verzinnen. Als het niet in KB staat — kb_missing.\n"
        "MATH: als het een rekenvraag is met inputs — reken exact zonder aannames.\n"
    ),
}

BUTTON_PROMPTS = {
    "what": {
        "ua": "Поясни: що таке Maison de Café. Формат, для кого, як працює, що входить у старт, що отримує партнер. Коротко.",
        "ru": "Поясни: что такое Maison de Café. Формат, для кого, как работает, что входит в старт, что получает партнёр. Коротко.",
        "en": "Explain what Maison de Café is: concept, for whom, how it works, what's included, what partner gets. Concise.",
        "fr": "Explique Maison de Café : concept, pour qui, fonctionnement, inclus, ce que reçoit le partenaire. Court.",
        "nl": "Leg Maison de Café uit: concept, voor wie, werking, inbegrepen, wat partner krijgt. Kort.",
    },
    "price": {
        "ua": "Відповідай про вартість відкриття. Структура витрат + що входить/не входить. Без порад.",
        "ru": "Ответь про стоимость открытия. Структура затрат + что входит/не входит. Без советов.",
        "en": "Opening cost: cost structure + included/not included. No generic tips.",
        "fr": "Coût d’ouverture : structure + inclus/non inclus. Pas de conseils généraux.",
        "nl": "Opstartkosten: structuur + inbegrepen/niet inbegrepen. Geen algemene tips.",
    },
    "payback": {
        "ua": "Окупність і прибуток. Приклад: маржа/чашка, чашок/день, 30 днів; валова маржа/міс; приклад витрат; логіка окупності.",
        "ru": "Окупаемость и прибыль. Пример: маржа/чашка, чашек/день, 30 дней; валовая маржа/мес; пример расходов; логика окупаемости.",
        "en": "Payback & profit. Example with margin/cup, cups/day, 30 days; gross margin/month; example costs; payback logic.",
        "fr": "Rentabilité & profit. Exemple avec marge/tasse, tasses/jour, 30 jours; marge brute/mois; coûts; logique ROI.",
        "nl": "Terugverdientijd & winst. Voorbeeld met marge/kop, koppen/dag, 30 dagen; brutomarge/maand; kosten; logica.",
    },
    "franchise": {
        "ua": "Умови співпраці/франшизи: підтримка, стандарти, зобов’язання партнера, сервіс. Без вигадок.",
        "ru": "Условия сотрудничества/франшизы: поддержка, стандарты, обязательства партнера, сервис. Без выдумок.",
        "en": "Franchise/partnership terms: support, standards, partner obligations, service. No inventions.",
        "fr": "Conditions franchise/partenariat : support, standards, obligations, service. Sans inventer.",
        "nl": "Franchisevoorwaarden: support, standaarden, verplichtingen, service. Niet verzinnen.",
    },
}

STRICT_KB_RULES = {
    "ua": (
        "КРИТИЧНО: відповідай ТІЛЬКИ з бази знань Maison de Café (File Search).\n"
        "ПЕРЕД ВІДПОВІДДЮ: обов’язково виконай File Search мінімум 1 раз.\n"
        "Якщо у KB нема відповіді — скажи kb_missing.\n"
        "Відповідай українською."
    ),
    "ru": (
        "КРИТИЧНО: отвечай ТОЛЬКО из базы знаний Maison de Café (File Search).\n"
        "ПЕРЕД ОТВЕТОМ: обязательно выполни File Search минимум 1 раз.\n"
        "Если в KB нет ответа — скажи kb_missing.\n"
        "Отвечай по-русски."
    ),
    "en": (
        "CRITICAL: answer ONLY from Maison de Café knowledge base (File Search).\n"
        "BEFORE ANSWERING: you MUST perform File Search at least once.\n"
        "If KB lacks the answer — say kb_missing.\n"
        "Answer in English."
    ),
    "fr": (
        "CRITIQUE : réponds UNIQUEMENT depuis la base Maison de Café (File Search).\n"
        "AVANT DE RÉPONDRE : tu DOIS faire un File Search au moins 1 fois.\n"
        "Si absent de la KB — kb_missing.\n"
        "Réponds en français."
    ),
    "nl": (
        "KRITISCH: antwoord ALLEEN uit de Maison de Café kennisbank (File Search).\n"
        "VOOR JE ANTWOORD: je MOET minimaal 1x File Search gebruiken.\n"
        "Als het niet in KB staat — kb_missing.\n"
        "Antwoord in het Nederlands."
    ),
}

def build_instructions(lang: str, action_key: Optional[str] = None) -> str:
    base = (
        HUMAN_CONSULTANT_RULES.get(lang, HUMAN_CONSULTANT_RULES["ua"])
        + "\n"
        + STRICT_KB_RULES.get(lang, STRICT_KB_RULES["ua"])
    )
    if action_key and action_key in BUTTON_PROMPTS:
        return base + "\n\nTASK:\n" + BUTTON_PROMPTS[action_key][lang]
    return base

def run_used_file_search(thread_id: str, run_id: str) -> bool:
    try:
        steps = client.beta.threads.runs.steps.list(thread_id=thread_id, run_id=run_id)
        for st in steps.data:
            details = getattr(st, "step_details", None)
            tool_calls = getattr(details, "tool_calls", None)
            if not tool_calls:
                continue
            for tc in tool_calls:
                # В Assistants API file_search обычно приходит как type="file_search"
                if getattr(tc, "type", "") == "file_search":
                    return True
        return False
    except Exception as e:
        print("RUN STEPS ERROR:", repr(e))
        return False

async def ask_assistant_strict(user_id: str, lang: str, user_text: str, action_key: Optional[str] = None) -> str:
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
        temperature=0,
    )

    while True:
        rs = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if rs.status == "completed":
            break
        if rs.status in ["failed", "cancelled", "expired"]:
            return ""
        await asyncio.sleep(0.7)

    # COMPLIANCE GATE: ответ разрешён только если реально был file_search
    if not run_used_file_search(thread_id=thread_id, run_id=run.id):
        return "kb_missing"

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return ""

    # Берём первое сообщение ассистента из последних
    for msg in messages.data:
        if getattr(msg, "role", "") == "assistant":
            try:
                return msg.content[0].text.value
            except Exception:
                continue
    return ""


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
# LEAD FORM FLOW (оставляем как есть)
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

        if email_sent:
            email_note = "✅ Email notification sent."
        else:
            email_note = (
                "Note: SMTP is not configured; owner was notified in Telegram."
                if owner_notified
                else "Note: SMTP not configured and owner Telegram notify failed."
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

        if user_id in lead_states:
            update.message.text = user_text
            handled = await handle_lead_form(update, context)
            if handled:
                return

        ai_reply = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=user_text, action_key=None)

        if ai_reply.strip() == "kb_missing" or not ai_reply:
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

    if user_id in blocked_users:
        return

    if is_gibberish_or_spam(text) or rate_limited(user_id):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["spam_stop"], reply_markup=mk_main_keyboard(lang))
        return

    if user_id in lead_states:
        handled = await handle_lead_form(update, context)
        if handled:
            return

    if is_language_button(text):
        await show_language_menu(update, context)
        return

    chosen = parse_lang_choice(text)
    if chosen:
        await set_language(update, context, chosen)
        return

    action = button_action_from_text(text)

    if is_contacts_button(text):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # Контентные кнопки: язык = язык кнопки (жёстко)
    if action and action[0] in {"what", "price", "payback", "franchise"}:
        action_key, button_lang = action
        user_lang[user_id] = button_lang

        command_text = f"[BUTTON:{action_key}] {MENU[button_lang][action_key]}"
        ai_reply = await ask_assistant_strict(
            user_id=user_id,
            lang=button_lang,
            user_text=command_text,
            action_key=action_key,
        )

        if ai_reply.strip() == "kb_missing" or not ai_reply:
            await update.message.reply_text(TEXTS[button_lang]["kb_missing"], reply_markup=mk_main_keyboard(button_lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(button_lang))
        return

    # Обычный вопрос
    lang = get_lang(user_id)
    try:
        ai_reply = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=text, action_key=None)

        if ai_reply.strip() == "kb_missing" or not ai_reply:
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))

    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

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
