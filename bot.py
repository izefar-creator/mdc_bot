import os
import io
import re
import time
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Dict, Optional, Tuple, Set, Any

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
# ВАЖНО: тред отдельный на язык, чтобы языки не мешали друг другу.
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
            "Я відповідаю як консультант і використовую лише офіційну базу знань Maison de Café.\n"
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
        "kb_missing": (
            "Я не знайшов відповіді у базі знань Maison de Café.\n"
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
            "Я отвечаю как консультант и использую только официальную базу знаний Maison de Café.\n"
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
        "kb_missing": (
            "Я не нашёл ответа в базе знаний Maison de Café.\n"
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
            "My name is Max, the virtual assistant of Maison de Café.\n"
            "I answer as a consultant and use only the official Maison de Café knowledge base.\n"
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
            "I couldn’t find the answer in the Maison de Café knowledge base.\n"
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
            "Je m’appelle Max, assistant virtuel de Maison de Café.\n"
            "Je réponds comme un consultant et j’utilise uniquement la base de connaissances officielle Maison de Café.\n"
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
            "Je n’ai pas trouvé la réponse dans la base de connaissances Maison de Café.\n"
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
            "Ik antwoord als consultant en gebruik alleen de officiële Maison de Café kennisbank.\n"
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
            "Ik kon het antwoord niet vinden in de Maison de Café kennisbank.\n"
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
# COMPLIANCE / KB HARD-GATE
# =========================
def _step_has_file_search(step: Any) -> bool:
    """
    В разных версиях SDK структура step может отличаться.
    Пытаемся устойчиво проверить наличие вызова file_search.
    """
    try:
        d = step.model_dump() if hasattr(step, "model_dump") else dict(step)
    except Exception:
        try:
            d = dict(step)
        except Exception:
            d = {}

    # Новые структуры обычно имеют step_details.tool_calls
    details = d.get("step_details") or {}
    tool_calls = details.get("tool_calls") or []

    for tc in tool_calls:
        # tc может быть dict или объект
        try:
            tcd = tc.model_dump() if hasattr(tc, "model_dump") else dict(tc)
        except Exception:
            try:
                tcd = dict(tc)
            except Exception:
                tcd = {}

        t = (tcd.get("type") or "").lower()
        if "file_search" in t:
            return True

        # Иногда название инструмента лежит глубже
        name = ""
        if isinstance(tcd.get("file_search"), dict):
            name = (tcd.get("file_search").get("name") or "").lower()
        if "file_search" in name:
            return True

    return False


def run_used_file_search(thread_id: str, run_id: str) -> bool:
    """
    Корпоративный KB-гейт:
    считаем ответ допустимым только если в steps был реальный file_search.
    """
    try:
        steps = client.beta.threads.runs.steps.list(thread_id=thread_id, run_id=run_id)
        for step in steps.data:
            if _step_has_file_search(step):
                return True
        return False
    except Exception as e:
        # Если steps недоступны/ошибка API — безопаснее считать, что file_search НЕ был.
        print("RUN STEPS CHECK ERROR:", repr(e))
        return False


def looks_bad_or_empty(ai_reply: str) -> bool:
    if not ai_reply:
        return True
    if len(ai_reply.strip()) < 2:
        return True
    if len(ai_reply) > 3500:
        return True
    return False


# =========================
# STRICT PROMPTS (BUTTONS) + HUMAN CONSULTANT RULES
# =========================
BUTTON_PROMPTS = {
    "what": {
        "ua": "Поясни: що таке Maison de Café. Дай чітко: формат, для кого, як працює, що входить у старт, що отримує партнер. Коротко, структуровано, “по-людськи”.",
        "ru": "Поясни: что такое Maison de Café. Дай чётко: формат, для кого, как работает, что входит в старт, что получает партнёр. Коротко, структурировано, “по-человечески”.",
        "en": "Explain what Maison de Café is: concept, who it is for, how it works, what is included in the start package, what the partner gets. Short, structured, human-like.",
        "fr": "Explique Maison de Café : concept, pour qui, comment ça marche, ce qui est inclus au démarrage, ce que reçoit le partenaire. Court, structuré, ton “consultant”.",
        "nl": "Leg Maison de Café uit: concept, voor wie, hoe het werkt, wat in de start zit, wat de partner krijgt. Kort, gestructureerd, menselijk.",
    },
    "price": {
        "ua": "Відповідай про вартість відкриття. Дай структуру: що входить / що не входить. Якщо є діапазони — назви їх. Не додавай нічого від себе.",
        "ru": "Ответь про стоимость открытия. Дай структуру: что входит / что не входит. Если есть диапазоны — назови их. Ничего от себя не добавляй.",
        "en": "Answer about opening cost. Provide structure: included / not included. If ranges exist, state them. Do not add anything beyond the KB.",
        "fr": "Réponds sur le coût d’ouverture : inclus / non inclus, et fourchettes si elles existent. N’ajoute rien au-delà de la base.",
        "nl": "Antwoord over opstartkosten: inbegrepen / niet inbegrepen, en ranges als ze bestaan. Voeg niets toe buiten de kennisbank.",
    },
    "payback": {
        "ua": "Відповідай тільки про окупність і прибуток. Якщо в базі є цифри — порахуй прозоро. Якщо цифр немає — скажи, що в базі немає точних даних і запропонуй залишити заявку.",
        "ru": "Отвечай только про окупаемость и прибыль. Если в базе есть цифры — посчитай прозрачно. Если цифр нет — скажи, что в базе нет точных данных и предложи оставить заявку.",
        "en": "Answer only about payback and profit. If the KB provides numbers, calculate transparently. If not, say the KB doesn’t contain precise numbers and suggest leaving a request.",
        "fr": "Réponds uniquement sur la rentabilité/profit. Si la base donne des chiffres, calcule clairement. Sinon, indique qu’il manque des données précises et propose de laisser une demande.",
        "nl": "Antwoord alleen over terugverdientijd/winst. Als de kennisbank cijfers heeft: reken transparant. Anders: zeg dat exacte cijfers ontbreken en stel een aanvraag voor.",
    },
    "franchise": {
        "ua": "Відповідай про умови співпраці/франшизи: формат, підтримка, зобов’язання партнера, стандарти, сервіс, обмеження. Без вигадок.",
        "ru": "Ответь про условия сотрудничества/франшизы: формат, поддержка, обязательства партнёра, стандарты, сервис, ограничения. Без выдумок.",
        "en": "Answer about franchise/partnership terms: format, support, partner obligations, standards, service, limitations. No inventions.",
        "fr": "Réponds sur les conditions franchise/partenariat : format, support, obligations, standards, service, limites. Sans inventer.",
        "nl": "Antwoord over franchise-/samenwerkingsvoorwaarden: format, support, verplichtingen, standaarden, service, beperkingen. Niet verzinnen.",
    },
}

HUMAN_CONSULTANT_RULES = {
    "ua": (
        "Ти — Макс, консультант Maison de Café.\n"
        "КРИТИЧНО: Відповідай ТІЛЬКИ з бази знань Maison de Café через File Search.\n"
        "ЖОДНИХ вигадок, домислів, загальних порад, і ЖОДНИХ інших бізнес-моделей (лише Maison de Café).\n"
        "Тон: коротко, ввічливо, “по-людськи”, структуровано (списки/пункти), як sales-консультант.\n"
        "Математика дозволена лише на основі цифр, що є в базі або надані користувачем; не придумуй нові цифри.\n"
        "Якщо в базі немає відповіді — прямо скажи, що не знайшов у базі, і запропонуй залишити заявку."
    ),
    "ru": (
        "Ты — Макс, консультант Maison de Café.\n"
        "КРИТИЧНО: Отвечай ТОЛЬКО из базы знаний Maison de Café через File Search.\n"
        "НИКАКИХ выдумок, догадок, общих советов и НИКАКИХ других бизнес-моделей (только Maison de Café).\n"
        "Тон: коротко, вежливо, “по-человечески”, структурировано (списки/пункты), как sales-консультант.\n"
        "Математика разрешена только на основе цифр из базы или цифр пользователя; новые цифры не придумывай.\n"
        "Если ответа нет — прямо скажи, что не нашёл в базе, и предложи оставить заявку."
    ),
    "en": (
        "You are Max, a Maison de Café consultant.\n"
        "CRITICAL: Answer ONLY from the Maison de Café knowledge base via File Search.\n"
        "No inventions, no guessing, no generic advice, and no other business models (Maison de Café only).\n"
        "Tone: short, polite, human-like, structured (bullets), like a sales consultant.\n"
        "Math is allowed only using numbers from the KB or provided by the user; do not create new numbers.\n"
        "If the KB doesn’t contain the answer, say so and suggest leaving a request."
    ),
    "fr": (
        "Tu es Max, consultant Maison de Café.\n"
        "CRITIQUE : Réponds UNIQUEMENT à partir de la base de connaissances Maison de Café via File Search.\n"
        "Aucune invention, aucun “conseil général”, aucun autre modèle business (Maison de Café uniquement).\n"
        "Ton : court, poli, humain, structuré (puces), comme un consultant commercial.\n"
        "Calculs autorisés uniquement avec les chiffres de la base ou fournis par l’utilisateur; n’invente pas de chiffres.\n"
        "Si la base ne contient pas la réponse, dis-le clairement et propose de laisser une demande."
    ),
    "nl": (
        "Je bent Max, consultant van Maison de Café.\n"
        "KRITISCH: Antwoord ALLEEN vanuit de Maison de Café kennisbank via File Search.\n"
        "Niet verzinnen, niet gokken, geen algemene adviezen, geen andere businessmodellen (alleen Maison de Café).\n"
        "Toon: kort, beleefd, menselijk, gestructureerd (bullets), als sales-consultant.\n"
        "Rekenen mag alleen met cijfers uit de kennisbank of van de gebruiker; verzin geen cijfers.\n"
        "Staat het niet in de kennisbank: zeg dat duidelijk en stel een aanvraag voor."
    ),
}


def build_instructions(lang: str, action_key: Optional[str] = None) -> str:
    base = HUMAN_CONSULTANT_RULES.get(lang, HUMAN_CONSULTANT_RULES["ua"])
    if action_key and action_key in BUTTON_PROMPTS:
        return base + "\n\nTASK:\n" + BUTTON_PROMPTS[action_key][lang]
    return base
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
# LEAD FORM FLOW
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
# STRICT KB ASK (HARD FILE_SEARCH GATE)
# =========================
async def ask_assistant_strict(
    user_id: str,
    lang: str,
    user_text: str,
    action_key: Optional[str] = None,
) -> Tuple[str, bool]:
    """
    Возвращает (answer, used_file_search).
    Корпоративный принцип: ответ валиден только если used_file_search == True.
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

    # polling
    while True:
        rs = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if rs.status == "completed":
            break
        if rs.status in ["failed", "cancelled", "expired"]:
            return ("", False)
        await asyncio.sleep(1)

    used_fs = run_used_file_search(thread_id=thread_id, run_id=run.id)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    if not messages.data:
        return ("", used_fs)

    # последнее сообщение ассистента обычно первым в списке
    try:
        answer = messages.data[0].content[0].text.value
    except Exception:
        answer = ""

    return (answer, used_fs)


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

        # иначе — строгий ассистент (HARD file_search gate)
        ai_reply, used_fs = await ask_assistant_strict(
            user_id=user_id,
            lang=lang,
            user_text=user_text,
            action_key=None,
        )

        if (not used_fs) or looks_bad_or_empty(ai_reply):
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

    # Лид-форма — локальная логика
    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # Контентные кнопки: what/price/payback/franchise
    if action and action[0] in {"what", "price", "payback", "franchise"}:
        action_key, button_lang = action

        # ЖЁСТКО: язык = язык кнопки
        user_lang[user_id] = button_lang

        # ВАЖНО: в user_text отправляем нормальный “вопрос”, чтобы file_search триггерился стабильно.
        # НЕ отправляем [BUTTON:...] — это ломало поисковое поведение.
        user_query = MENU[button_lang][action_key]

        ai_reply, used_fs = await ask_assistant_strict(
            user_id=user_id,
            lang=button_lang,
            user_text=user_query,
            action_key=action_key,
        )

        # HARD GATE
        if (not used_fs) or looks_bad_or_empty(ai_reply):
            await update.message.reply_text(TEXTS[button_lang]["kb_missing"], reply_markup=mk_main_keyboard(button_lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(button_lang))
        return

    # Обычный текстовый вопрос
    lang = get_lang(user_id)
    try:
        ai_reply, used_fs = await ask_assistant_strict(
            user_id=user_id,
            lang=lang,
            user_text=text,
            action_key=None,
        )

        if (not used_fs) or looks_bad_or_empty(ai_reply):
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
