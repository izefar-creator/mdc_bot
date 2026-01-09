# =========================
# bot.py (PART 1/3)
# =========================

import os
import re
import sys
import json
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

import fcntl  # Linux-only; OK for Render
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mdc_bot")

# -------------------------
# Hard process lock (Variant B)
# Prevent 2nd instance from running (avoids telegram.error.Conflict)
# -------------------------
_LOCK_FH = None

def acquire_single_instance_lock(lock_path: str = "/tmp/mdc_bot.lock") -> None:
    global _LOCK_FH
    _LOCK_FH = open(lock_path, "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH.write(str(os.getpid()))
        _LOCK_FH.flush()
        log.info("Single-instance lock acquired: %s", lock_path)
    except BlockingIOError:
        log.error("Another instance is already running. Exiting immediately.")
        sys.exit(0)

# -------------------------
# Config
# -------------------------
@dataclass(frozen=True)
class Config:
    TELEGRAM_TOKEN: str
    OPENAI_API_KEY: str
    ASSISTANT_ID: str

    # Optional: if you want to push lead-lite to owner chat/channel
    BOT_OWNER_CHAT_ID: Optional[int]

    # Presentation PDF file_id
    PRESENTATION_FILE_ID: Optional[str]

    # Polling vs webhook
    MODE: str  # "polling" or "webhook"
    WEBHOOK_URL: Optional[str]
    WEBHOOK_PATH: str
    PORT: int

    # Verification (optional)
    ENABLE_VERIFIER: bool
    VERIFIER_MODEL: str

def load_config() -> Config:
    def get_env(name: str):
        v = os.getenv(name)
        return v.strip() if v and v.strip() else None

    token = (
        get_env("TELEGRAM_TOKEN")
        or get_env("TELEGRAM_BOT_TOKEN")
        or get_env("BOT_TOKEN")
    )

    if not token:
        raise RuntimeError(
            "Missing Telegram token. Checked: TELEGRAM_TOKEN, TELEGRAM_BOT_TOKEN, BOT_TOKEN"
        )


    cfg = Config(
        TELEGRAM_TOKEN=token,
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY", "").strip(),
        ASSISTANT_ID=os.getenv("ASSISTANT_ID", "").strip(),
        BOT_OWNER_CHAT_ID=int(os.getenv("BOT_OWNER_CHAT_ID")) if os.getenv("BOT_OWNER_CHAT_ID") else None,
        PRESENTATION_FILE_ID=os.getenv("PRESENTATION_FILE_ID", "").strip() or None,
        MODE=(os.getenv("MODE", "polling").strip().lower()),
        WEBHOOK_URL=os.getenv("WEBHOOK_URL", "").strip() or None,
        WEBHOOK_PATH=os.getenv("WEBHOOK_PATH", "/telegram").strip(),
        PORT=int(os.getenv("PORT", "10000")),
        ENABLE_VERIFIER=(os.getenv("ENABLE_VERIFIER", "1").strip() == "1"),
        VERIFIER_MODEL=os.getenv("VERIFIER_MODEL", "gpt-4.1-mini").strip(),
    )

    if not cfg.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY is empty. Bot will not be able to generate AI answers.")
    if not cfg.ASSISTANT_ID:
        log.warning("ASSISTANT_ID is empty. Bot will not be able to call your Assistant.")
    return cfg

CFG = None  # set in main()

# -------------------------
# Language / UX
# -------------------------
LANG_UA = "uk"
LANG_RU = "ru"
LANG_EN = "en"
LANG_FR = "fr"

LANG_LABELS = {
    LANG_UA: "Українська",
    LANG_RU: "Русский",
    LANG_EN: "English",
    LANG_FR: "Français",
}

# Reply-menu keys (internal)
MENU_ABOUT = "about"
MENU_COST = "cost"
MENU_PAYBACK = "payback"
MENU_TERMS = "terms"
MENU_CONTACTS = "contacts"
MENU_LEAD = "lead"
MENU_PRESENTATION = "presentation"
MENU_LANGUAGE = "language"

# Per-language captions for reply buttons
MENU_TEXT = {
    LANG_UA: {
        MENU_ABOUT: "☕️ Що таке Maison de Café?",
        MENU_COST: "💶 Скільки коштує відкрити?",
        MENU_PAYBACK: "📈 Окупність і прибуток",
        MENU_TERMS: "🤝 Умови співпраці",
        MENU_CONTACTS: "📞 Контакти",
        MENU_LEAD: "📝 Залишити заявку",
        MENU_PRESENTATION: "📄 Презентація",
        MENU_LANGUAGE: "🌍 Мова",
    },
    LANG_RU: {
        MENU_ABOUT: "☕️ Что такое Maison de Café?",
        MENU_COST: "💶 Сколько стоит открыть кофейню?",
        MENU_PAYBACK: "📈 Окупаемость и прибыль",
        MENU_TERMS: "🤝 Условия франшизы",
        MENU_CONTACTS: "📞 Контакты / связь с владельцем",
        MENU_LEAD: "📝 Оставить заявку",
        MENU_PRESENTATION: "📄 Презентация",
        MENU_LANGUAGE: "🌍 Язык / Language",
    },
    LANG_EN: {
        MENU_ABOUT: "☕️ What is Maison de Café?",
        MENU_COST: "💶 Opening cost",
        MENU_PAYBACK: "📈 Payback & profit",
        MENU_TERMS: "🤝 Partnership terms",
        MENU_CONTACTS: "📞 Contacts",
        MENU_LEAD: "📝 Leave a request",
        MENU_PRESENTATION: "📄 Presentation",
        MENU_LANGUAGE: "🌍 Language",
    },
    LANG_FR: {
        MENU_ABOUT: "☕️ Qu’est-ce que Maison de Café ?",
        MENU_COST: "💶 Coût d’ouverture",
        MENU_PAYBACK: "📈 Rentabilité & profit",
        MENU_TERMS: "🤝 Conditions de partenariat",
        MENU_CONTACTS: "📞 Contacts",
        MENU_LEAD: "📝 Laisser une demande",
        MENU_PRESENTATION: "📄 Présentation",
        MENU_LANGUAGE: "🌍 Langue",
    },
}

def get_lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("lang", LANG_UA)

def set_lang(ctx: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    if lang not in (LANG_UA, LANG_RU, LANG_EN, LANG_FR):
        lang = LANG_UA
    ctx.user_data["lang"] = lang

def build_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    t = MENU_TEXT[lang]
    # 6–7 buttons (+ Language)
    rows = [
        [t[MENU_ABOUT], t[MENU_COST]],
        [t[MENU_PAYBACK], t[MENU_TERMS]],
        [t[MENU_CONTACTS], t[MENU_LEAD]],
        [t[MENU_PRESENTATION]],
        [t[MENU_LANGUAGE]],
    ]
    # one_time_keyboard=True -> after press it hides (square appears)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=False,
    )

def build_language_inline_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:uk"),
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
        ],
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇫🇷 Français", callback_data="lang:fr"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

def t(lang: str, key: str) -> str:
    # Minimal strings; keep professional and short
    base = {
        "hello": {
            LANG_UA: "Привіт! Я Макс, консультант Maison de Café.\nОбери пункт меню або просто напиши питання — я відповім по суті.",
            LANG_RU: "Привет! Я Макс, консультант Maison de Café.\nВыбери пункт меню или просто напиши вопрос — я отвечу по сути.",
            LANG_EN: "Hi! I’m Max, Maison de Café consultant.\nChoose a menu item or just ask a question — I’ll answer to the point.",
            LANG_FR: "Bonjour ! Je suis Max, consultant Maison de Café.\nChoisissez un пункт меню ou posez une question — je répondrai concrètement.",
        },
        "choose_lang": {
            LANG_UA: "Оберіть мову:",
            LANG_RU: "Выберите язык:",
            LANG_EN: "Choose language:",
            LANG_FR: "Choisissez la langue :",
        },
        "lang_updated": {
            LANG_UA: "Мову змінено.",
            LANG_RU: "Язык обновлён.",
            LANG_EN: "Language updated.",
            LANG_FR: "Langue mise à jour.",
        },
        "no_presentation": {
            LANG_UA: "Презентація тимчасово недоступна.",
            LANG_RU: "Презентация временно недоступна.",
            LANG_EN: "Presentation is temporarily unavailable.",
            LANG_FR: "La présentation est temporairement indisponible.",
        },
        "lead_start": {
            LANG_UA: "Добре. Напиши, будь ласка, твоє ім’я.",
            LANG_RU: "Хорошо. Напишите, пожалуйста, ваше имя.",
            LANG_EN: "Sure. Please share your name.",
            LANG_FR: "D’accord. Indiquez votre nom, s’il vous plaît.",
        },
        "lead_city": {
            LANG_UA: "Дякую. Яке місто/район?",
            LANG_RU: "Спасибо. Какой город/район?",
            LANG_EN: "Thanks. What city/area?",
            LANG_FR: "Merci. Quelle ville/quartier ?",
        },
        "lead_phone": {
            LANG_UA: "Супер. Телефон або Telegram-нік для зв’язку?",
            LANG_RU: "Отлично. Телефон или Telegram-ник для связи?",
            LANG_EN: "Great. Phone or Telegram username?",
            LANG_FR: "Parfait. Téléphone ou pseudo Telegram ?",
        },
        "lead_done": {
            LANG_UA: "Дякую! Заявку прийнято. Ми зв’яжемося з тобою найближчим часом.",
            LANG_RU: "Спасибо! Заявка принята. Мы свяжемся с вами в ближайшее время.",
            LANG_EN: "Thank you! Request received. We’ll contact you shortly.",
            LANG_FR: "Merci ! Demande reçue. Nous vous contacterons rapidement.",
        },
        "kb_missing": {
            LANG_UA: "Щоб відповісти точно, мені потрібні дані по цьому пункту. Скажіть, будь ласка, що саме цікавить — і я уточню.",
            LANG_RU: "Чтобы ответить точно, мне нужны данные по этому пункту. Уточните, пожалуйста, что именно интересует — и я отвечу корректно.",
            LANG_EN: "To answer precisely, I need a bit more detail. Please уточните what exactly you want to know.",
            LANG_FR: "Pour répondre précisément, j’ai besoin de détails. Précisez votre question, s’il vous plaît.",
        },
    }
    return base[key][lang]

# -------------------------
# Calculator (constants)
# -------------------------
MARGIN_PER_CUP = 1.8
DAYS_PER_MONTH = 30
EXPENSES_LOW = 450
EXPENSES_HIGH = 600
CUPS_MIN = 1
CUPS_MAX = 200

def extract_cups_per_day(text: str) -> Optional[int]:
    """
    Extract cups/day from free text for calculator.
    Accepts 1..200.
    """
    if not text:
        return None

    # must contain hint words OR be like "40 cups"
    hint = re.search(r"(чаш|cups?|tasses?)", text, re.IGNORECASE)
    nums = re.findall(r"\b(\d{1,3})\b", text)
    if not nums:
        return None
    for n in nums:
        v = int(n)
        if CUPS_MIN <= v <= CUPS_MAX:
            # if user mentioned cups or explicitly asked about earnings/profit -> accept
            if hint or re.search(r"(прибыл|доход|заработ|profit|earn|rentab|оккуп|окуп)", text, re.IGNORECASE):
                return v
    return None

def calc_profit(cups_per_day: int) -> Dict[str, float]:
    gross_day = cups_per_day * MARGIN_PER_CUP
    gross_month = gross_day * DAYS_PER_MONTH
    net_low = gross_month - EXPENSES_HIGH
    net_high = gross_month - EXPENSES_LOW
    return {
        "gross_day": gross_day,
        "gross_month": gross_month,
        "net_low": net_low,
        "net_high": net_high,
    }

def format_calc(lang: str, cups: int, r: Dict[str, float]) -> str:
    # Style: Max, short, to the point
    if lang == LANG_RU:
        return (
            f"Хороший вопрос — давайте посчитаем.\n\n"
            f"• Чашек в день: {cups}\n"
            f"• Средняя маржа с чашки: {MARGIN_PER_CUP:.1f} €\n"
            f"• Валовая маржа/день: {r['gross_day']:.0f} €\n"
            f"• Валовая маржа/месяц (30 дней): {r['gross_month']:.0f} €\n"
            f"• Минус средние расходы (450–600 €/мес):\n"
            f"  ⇒ ориентир чистыми: {r['net_low']:.0f}–{r['net_high']:.0f} € / мес\n\n"
            f"Если хочешь — напиши другую цифру (1–200 чашек/день), пересчитаю."
        )
    if lang == LANG_EN:
        return (
            f"Good question — let’s calculate.\n\n"
            f"• Cups/day: {cups}\n"
            f"• Avg margin per cup: {MARGIN_PER_CUP:.1f} €\n"
            f"• Gross margin/day: {r['gross_day']:.0f} €\n"
            f"• Gross margin/month (30 days): {r['gross_month']:.0f} €\n"
            f"• Minus average monthly expenses (450–600 €):\n"
            f"  ⇒ estimated net: {r['net_low']:.0f}–{r['net_high']:.0f} € / month\n\n"
            f"Send another number (1–200 cups/day) and I’ll recalculate."
        )
    if lang == LANG_FR:
        return (
            f"Bonne question — calculons.\n\n"
            f"• Tasses/jour : {cups}\n"
            f"• Marge moyenne par tasse : {MARGIN_PER_CUP:.1f} €\n"
            f"• Marge brute/jour : {r['gross_day']:.0f} €\n"
            f"• Marge brute/mois (30 jours) : {r['gross_month']:.0f} €\n"
            f"• Moins dépenses mensuelles (450–600 €) :\n"
            f"  ⇒ net estimé : {r['net_low']:.0f}–{r['net_high']:.0f} € / mois\n\n"
            f"Envoie un autre chiffre (1–200 tasses/jour) et je recalcule."
        )
    # UA default
    return (
        f"Хороший запит — давайте порахуємо.\n\n"
        f"• Чашок на день: {cups}\n"
        f"• Середня маржа з чашки: {MARGIN_PER_CUP:.1f} €\n"
        f"• Валова маржа/день: {r['gross_day']:.0f} €\n"
        f"• Валова маржа/місяць (30 днів): {r['gross_month']:.0f} €\n"
        f"• Мінус середні витрати (450–600 €/міс):\n"
        f"  ⇒ орієнтир чистими: {r['net_low']:.0f}–{r['net_high']:.0f} € / міс\n\n"
        f"Хочеш — напиши іншу цифру (1–200 чашок/день), перерахую."
    )

# -------------------------
# Lead-lite state
# -------------------------
LEAD_STEP_KEY = "lead_step"
LEAD_DATA_KEY = "lead_data"
LEAD_STEP_NONE = 0
LEAD_STEP_NAME = 1
LEAD_STEP_CITY = 2
LEAD_STEP_PHONE = 3

def start_lead(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data[LEAD_STEP_KEY] = LEAD_STEP_NAME
    ctx.user_data[LEAD_DATA_KEY] = {}

def reset_lead(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data[LEAD_STEP_KEY] = LEAD_STEP_NONE
    ctx.user_data[LEAD_DATA_KEY] = {}

def get_lead_step(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return int(ctx.user_data.get(LEAD_STEP_KEY, LEAD_STEP_NONE))

# =========================
# bot.py (PART 2/3)
# =========================

# OpenAI SDK (new)
# pip install openai
try:
    from openai import OpenAI
except Exception as e:
    OpenAI = None
    log.warning("OpenAI SDK not available: %s", e)

def get_openai_client() -> Optional["OpenAI"]:
    if not CFG.OPENAI_API_KEY or OpenAI is None:
        return None
    return OpenAI(api_key=CFG.OPENAI_API_KEY)

# -------------------------
# Anti-hallucination / "anti-chush" guardrails (lightweight)
# -------------------------
FORBIDDEN_MARKERS = [
    # You can extend; keep conservative to avoid false positives
    "royalty",
    "паушальный",
    "паушал",
    "роялти",
]

# allowed numbers baseline (besides calculator which bypasses)
ALLOWED_NUMBERS = {
    "9800", "9 800",
    "1.8", "1,8",
    "35",
    "50",  # service fee, terminal rent fee
    "75",  # delivery days
    "12",  # warranty months
    "2",   # service contract years
}

def contains_forbidden_markers(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in FORBIDDEN_MARKERS)

def has_suspicious_numbers(text: str) -> bool:
    """
    Reject numbers that are not in allowed list.
    Note: Calculator branch bypasses this completely.
    """
    if not text:
        return False
    nums = re.findall(r"(\d+[.,]?\d*)", text)
    for n in nums:
        nn = n.strip()
        # normalize
        nn = nn.replace(",", ".")
        if nn in ALLOWED_NUMBERS:
            continue
        # Allow year-like? better to be strict:
        return True
    return False

def validate_answer(text: str) -> bool:
    if not text or len(text.strip()) < 2:
        return False
    if contains_forbidden_markers(text):
        return False
    if has_suspicious_numbers(text):
        return False
    return True

# -------------------------
# GOLD fallback answers (minimal, safe)
# (Do NOT mention KB/files/search)
# -------------------------
def gold_fallback(lang: str) -> str:
    if lang == LANG_RU:
        return "Хороший вопрос. Чтобы ответить точно и без ошибок, уточни, пожалуйста, один момент: что именно ты хочешь узнать по этому пункту?"
    if lang == LANG_EN:
        return "Good question. To answer precisely (without guessing), please уточните what exactly you want to know."
    if lang == LANG_FR:
        return "Bonne question. Pour répondre précisément (sans supposer), précisez votre demande, s’il vous plaît."
    return "Хороший запит. Щоб відповісти точно (без здогадок), уточни, будь ласка, що саме цікавить?"

# -------------------------
# Assistant call with FILE SEARCH gate
# -------------------------
def run_assistant_with_gate(user_text: str, lang: str) -> Tuple[bool, str]:
    """
    Returns: (ok, answer)
    ok=True only if file_search tool was actually used in the run steps.
    """
    client = get_openai_client()
    if client is None or not CFG.ASSISTANT_ID:
        return False, gold_fallback(lang)

    # Create thread
    thread = client.beta.threads.create()

    # User msg
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_text,
    )

    # Run assistant
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=CFG.ASSISTANT_ID,
    )

    # Poll
    while True:
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
        if run.status in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(0.6)

    if run.status != "completed":
        log.warning("Run not completed: %s", run.status)
        return False, gold_fallback(lang)

    # Inspect steps to ensure file_search was used
    used_file_search = False
    try:
        steps = client.beta.threads.runs.steps.list(thread_id=thread.id, run_id=run.id)
        for s in steps.data:
            sd = s.model_dump() if hasattr(s, "model_dump") else {}
            details = sd.get("step_details", {})
            tool_calls = details.get("tool_calls", []) or []
            for tc in tool_calls:
                # tc: {"type": "...", ...}
                if (tc.get("type") == "file_search") or ("file_search" in json.dumps(tc).lower()):
                    used_file_search = True
                    break
            if used_file_search:
                break
    except Exception as e:
        log.warning("Unable to inspect steps for file_search: %s", e)
        used_file_search = False

    # Get final assistant message
    answer_text = ""
    try:
        msgs = client.beta.threads.messages.list(thread_id=thread.id, limit=10)
        for m in msgs.data:
            if m.role == "assistant":
                # take the latest assistant message content
                parts = []
                for c in m.content:
                    if c.type == "text":
                        parts.append(c.text.value)
                answer_text = "\n".join(parts).strip()
                break
    except Exception as e:
        log.warning("Unable to read assistant messages: %s", e)
        answer_text = ""

    if not used_file_search:
        return False, gold_fallback(lang)

    if not answer_text:
        return False, gold_fallback(lang)

    return True, answer_text

# -------------------------
# Optional verifier (2nd pass) — does NOT write into assistant thread
# -------------------------
def verify_and_rewrite(answer: str, user_text: str, lang: str) -> str:
    if not CFG.ENABLE_VERIFIER:
        return answer

    client = get_openai_client()
    if client is None:
        return answer

    # Keep strict: rewrite to be safe, avoid forbidden markers & stray numbers.
    system = (
        "You are a strict QA verifier. "
        "Rewrite the answer to be factual, concise, and consistent with the user's business constraints. "
        "Do not invent numbers or terms. "
        "Avoid royalties/паушальный/роялти. "
        "If unsure, ask one short clarifying question. "
        "Return only the final answer text."
    )

    try:
        resp = client.responses.create(
            model=CFG.VERIFIER_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"USER QUESTION:\n{user_text}\n\nDRAFT ANSWER:\n{answer}\n\nLANGUAGE: {lang}"},
            ],
        )
        out = resp.output_text.strip() if hasattr(resp, "output_text") else ""
        if out:
            return out
        return answer
    except Exception as e:
        log.warning("Verifier failed: %s", e)
        return answer

# -------------------------
# Presentation sender
# -------------------------
async def send_presentation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(ctx)
    if not CFG.PRESENTATION_FILE_ID:
        await update.message.reply_text(t(lang, "no_presentation"), reply_markup=ReplyKeyboardRemove())
        return
    # Send by file_id
    try:
        await update.message.reply_document(
            document=CFG.PRESENTATION_FILE_ID,
            caption=None,
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception as e:
        log.warning("Failed to send presentation: %s", e)
        await update.message.reply_text(t(lang, "no_presentation"), reply_markup=ReplyKeyboardRemove())

# -------------------------
# Voice -> transcription
# -------------------------
async def transcribe_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    client = get_openai_client()
    if client is None:
        return None

    if not update.message or not update.message.voice:
        return None

    try:
        tg_file = await ctx.bot.get_file(update.message.voice.file_id)
        # Download to tmp
        local_path = f"/tmp/voice_{update.message.voice.file_unique_id}.ogg"
        await tg_file.download_to_drive(custom_path=local_path)

        # Whisper transcription
        with open(local_path, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        text = (tr.text or "").strip()
        return text or None
    except Exception as e:
        log.warning("Voice transcription failed: %s", e)
        return None

# =========================
# bot.py (PART 3/3)
# =========================

def match_menu(text: str, lang: str) -> Optional[str]:
    if not text:
        return None
    for key, caption in MENU_TEXT[lang].items():
        if text.strip() == caption:
            return key
    return None

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(ctx)
    reset_lead(ctx)
    await update.message.reply_text(
        t(lang, "hello"),
        reply_markup=build_reply_keyboard(lang),
    )

async def on_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if data.startswith("lang:"):
        lang = data.split(":", 1)[1].strip()
        set_lang(ctx, lang)

        # After language change -> show reply keyboard again (and only now)
        await query.message.reply_text(
            t(get_lang(ctx), "lang_updated"),
            reply_markup=build_reply_keyboard(get_lang(ctx)),
        )
        return

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    lang = get_lang(ctx)
    text = update.message.text.strip()

    # 0) Lead-lite flow (3 steps) has priority once started
    step = get_lead_step(ctx)
    if step != LEAD_STEP_NONE:
        lead_data = ctx.user_data.get(LEAD_DATA_KEY, {})
        if step == LEAD_STEP_NAME:
            lead_data["name"] = text
            ctx.user_data[LEAD_DATA_KEY] = lead_data
            ctx.user_data[LEAD_STEP_KEY] = LEAD_STEP_CITY
            await update.message.reply_text(t(lang, "lead_city"), reply_markup=ReplyKeyboardRemove())
            return
        if step == LEAD_STEP_CITY:
            lead_data["city"] = text
            ctx.user_data[LEAD_DATA_KEY] = lead_data
            ctx.user_data[LEAD_STEP_KEY] = LEAD_STEP_PHONE
            await update.message.reply_text(t(lang, "lead_phone"), reply_markup=ReplyKeyboardRemove())
            return
        if step == LEAD_STEP_PHONE:
            lead_data["contact"] = text
            ctx.user_data[LEAD_DATA_KEY] = lead_data
            reset_lead(ctx)

            # Notify owner (optional)
            if CFG.BOT_OWNER_CHAT_ID:
                try:
                    msg = (
                        "NEW LEAD (Maison de Café)\n"
                        f"Name: {lead_data.get('name')}\n"
                        f"City/Area: {lead_data.get('city')}\n"
                        f"Contact: {lead_data.get('contact')}\n"
                        f"From user: {update.effective_user.id} @{update.effective_user.username}"
                    )
                    await ctx.bot.send_message(chat_id=CFG.BOT_OWNER_CHAT_ID, text=msg)
                except Exception as e:
                    log.warning("Failed to notify owner: %s", e)

            await update.message.reply_text(t(lang, "lead_done"), reply_markup=ReplyKeyboardRemove())
            return

    # 1) Calculator branch (NO OpenAI, NO numeric filters)
    cups = extract_cups_per_day(text)
    if cups is not None:
        r = calc_profit(cups)
        await update.message.reply_text(
            format_calc(lang, cups, r),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # 2) Menu handling (reply buttons)
    menu_key = match_menu(text, lang)
    if menu_key == MENU_LANGUAGE:
        # Inline only for language
        await update.message.reply_text(
            t(lang, "choose_lang"),
            reply_markup=build_language_inline_keyboard(),
        )
        return

    if menu_key == MENU_PRESENTATION:
        await send_presentation(update, ctx)
        return

    if menu_key == MENU_LEAD:
        start_lead(ctx)
        await update.message.reply_text(t(lang, "lead_start"), reply_markup=ReplyKeyboardRemove())
        return

    # For menu clicks other than above, we still route to Assistant with a short prompt hint
    if menu_key in (MENU_ABOUT, MENU_COST, MENU_PAYBACK, MENU_TERMS, MENU_CONTACTS):
        # keep user’s language; do not add extra numbers
        user_text = text
    else:
        user_text = text

    # 3) AI answer branch (Assistant → gate → verifier → validation → fallback)
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    ok, draft = run_assistant_with_gate(user_text=user_text, lang=lang)
    if ok:
        final = verify_and_rewrite(draft, user_text, lang)
        if validate_answer(final):
            await update.message.reply_text(final, reply_markup=ReplyKeyboardRemove())
            return
        # If verifier produced something still suspicious, fallback
        await update.message.reply_text(gold_fallback(lang), reply_markup=ReplyKeyboardRemove())
        return

    # Gate failed -> fallback
    await update.message.reply_text(gold_fallback(lang), reply_markup=ReplyKeyboardRemove())

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(ctx)
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    text = await transcribe_voice(update, ctx)
    if not text:
        await update.message.reply_text(gold_fallback(lang), reply_markup=ReplyKeyboardRemove())
        return

    # Calculator also works for voice
    cups = extract_cups_per_day(text)
    if cups is not None:
        r = calc_profit(cups)
        await update.message.reply_text(format_calc(lang, cups, r), reply_markup=ReplyKeyboardRemove())
        return

    ok, draft = run_assistant_with_gate(user_text=text, lang=lang)
    if ok:
        final = verify_and_rewrite(draft, text, lang)
        if validate_answer(final):
            await update.message.reply_text(final, reply_markup=ReplyKeyboardRemove())
            return
    await update.message.reply_text(gold_fallback(lang), reply_markup=ReplyKeyboardRemove())

def build_app() -> Application:
    application = Application.builder().token(CFG.TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_callback_query))

    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application

def main() -> None:
    global CFG
    acquire_single_instance_lock()  # Variant B
    CFG = load_config()

    app = build_app()

    if CFG.MODE == "webhook":
        if not CFG.WEBHOOK_URL:
            raise RuntimeError("MODE=webhook but WEBHOOK_URL is empty")
        # Start webhook
        log.info("Starting webhook on port %s, path %s", CFG.PORT, CFG.WEBHOOK_PATH)
        app.run_webhook(
            listen="0.0.0.0",
            port=CFG.PORT,
            url_path=CFG.WEBHOOK_PATH.lstrip("/"),
            webhook_url=f"{CFG.WEBHOOK_URL.rstrip('/')}/{CFG.WEBHOOK_PATH.lstrip('/')}",
        )
    else:
        # Polling
        log.info("Starting polling")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

if __name__ == "__main__":
    main()

