import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")).strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "").strip()

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID", "").strip()
PRESENTATION_FILE_ID = os.getenv("PRESENTATION_FILE_ID", "").strip()  # Telegram file_id for the presentation PDF

VERIFY_MODEL = os.getenv("VERIFY_MODEL", "gpt-4o-mini").strip()
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-1").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID missing")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("maisonbot")


def mask_token(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 10:
        return tok
    return f"{tok[:4]}…{tok[-6:]}"


log.info("Boot: TELEGRAM token=%s", mask_token(TELEGRAM_BOT_TOKEN))
log.info("Boot: ASSISTANT_ID=%s", ASSISTANT_ID)


# =========================
# SINGLE INSTANCE LOCK (variant B)
# =========================
def acquire_single_instance_lock() -> None:
    """
    Prevents running 2 polling processes at the same time.
    Variant B: file lock. If locked -> exit immediately.
    """
    lock_path = os.getenv("BOT_LOCK_PATH", "/tmp/maisondecafe_bot.lock")
    try:
        import fcntl  # Linux/Unix only (Render = OK)
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        # Keep reference alive for the process lifetime
        globals()["_LOCK_FH"] = fh
        log.info("Single-instance lock acquired: %s", lock_path)
    except BlockingIOError:
        log.error("Another bot process is already running (lock busy). Exiting.")
        raise SystemExit(0)
    except Exception as e:
        # If lock fails unexpectedly, still allow running (but log it)
        log.warning("Single-instance lock not active (%s). Continuing.", e)


# =========================
# STATE (persisted)
# =========================
STATE_FILE = Path("maisonbot_state.json")


@dataclass
class UserState:
    lang: str = "RU"       # UA/RU/EN/FR
    thread_id: str = ""    # per-user shared thread


_state: Dict[str, UserState] = {}
_blocked = set()
_user_locks: Dict[str, asyncio.Lock] = {}


def load_state() -> None:
    global _state, _blocked
    if not STATE_FILE.exists():
        _state = {}
        _blocked = set()
        return
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    _blocked = set(raw.get("blocked", []))
    users = raw.get("users", {})
    _state = {uid: UserState(**users[uid]) for uid in users}


def save_state() -> None:
    raw = {
        "blocked": sorted(_blocked),
        "users": {uid: {"lang": s.lang, "thread_id": s.thread_id} for uid, s in _state.items()},
    }
    STATE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user(user_id: str) -> UserState:
    if user_id not in _state:
        _state[user_id] = UserState()
        save_state()
    return _state[user_id]


def get_user_lock(user_id: str) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


LANGS = ["UA", "RU", "EN", "FR"]

LANG_LABELS = {
    "UA": "🇺🇦 Українська",
    "RU": "🇷🇺 Русский",
    "EN": "🇬🇧 English",
    "FR": "🇫🇷 Français",
}

# 7 reply-buttons (no lead button; lead-lite stays via free text flow)
MENU_LABELS = {
    "UA": {
        "what": "☕ Що таке Maison de Café?",
        "price": "💶 Скільки коштує відкрити?",
        "payback": "📈 Окупність і прибуток",
        "terms": "🤝 Умови співпраці",
        "contacts": "📞 Контакти / наступний крок",
        "presentation": "📄 Презентація",
        "lang": "🌍 Мова",
    },
    "RU": {
        "what": "☕ Что такое Maison de Café?",
        "price": "💶 Сколько стоит открыть?",
        "payback": "📈 Окупаемость и прибыль",
        "terms": "🤝 Условия сотрудничества",
        "contacts": "📞 Контакты / следующий шаг",
        "presentation": "📄 Презентация",
        "lang": "🌍 Язык",
    },
    "EN": {
        "what": "☕ What is Maison de Café?",
        "price": "💶 Opening cost",
        "payback": "📈 Payback & profit",
        "terms": "🤝 Partnership terms",
        "contacts": "📞 Contacts / next step",
        "presentation": "📄 Presentation",
        "lang": "🌍 Language",
    },
    "FR": {
        "what": "☕ Qu’est-ce que Maison de Café ?",
        "price": "💶 Coût de lancement",
        "payback": "📈 Rentabilité & profit",
        "terms": "🤝 Conditions",
        "contacts": "📞 Contacts / prochaine étape",
        "presentation": "📄 Présentation",
        "lang": "🌍 Langue",
    },
}

CONTACTS_TEXT = {
    "UA": "Контакти Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "RU": "Контакты Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "EN": "Maison de Café contacts:\n• Email: maisondecafe.coffee@gmail.com\n• Phone: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "FR": "Contacts Maison de Café:\n• Email : maisondecafe.coffee@gmail.com\n• Téléphone : +32 470 600 806\n• Telegram : https://t.me/maisondecafe",
}

# GOLD answers (5 эталонов) — максимально близко к твоей формулировке.
GOLD_5 = {
    "RU": {
        "what": (
            "Хороший вопрос, с него обычно и начинается знакомство. "
            "Maison de Café — это готовая точка самообслуживания под ключ в Бельгии. "
            "Вы получаете профессиональный кофейный автомат Jetinno JL-300, фирменную стойку, систему контроля и стартовый набор ингредиентов, "
            "а также обучение и сопровождение запуска. Формат рассчитан на быстрый старт без опыта в кофейном бизнесе и работу без персонала. "
            "Дальше логично либо разобрать стоимость запуска, либо посмотреть на окупаемость и реальные цифры."
        ),
        "price": (
            "Хороший вопрос, давайте детально разберем. "
            "Базовая стоимость запуска точки Maison de Café в Бельгии составляет 9 800 €. "
            "В эту сумму входит профессиональный кофейный автомат Jetinno JL-300, фирменная стойка, телеметрия, стартовый набор ингредиентов, "
            "обучение и полный запуск. Это не франшиза с пакетами и скрытыми платежами — вы платите за конкретное оборудование и сервис. "
            "Отдельно обычно учитываются только вещи, зависящие от вашей ситуации, например аренда локации или электричество. "
            "Дальше логично либо посмотреть окупаемость, либо обсудить вашу будущую локацию."
        ),
        "payback": (
            "Хороший вопрос, без понимания цифр действительно нет смысла идти дальше. "
            "В базовой модели Maison de Café средняя маржа с одной чашки составляет около 1,8 €, а типичный объём продаж — примерно 35 чашек в день. "
            "Это даёт валовую маржу порядка 1 900 € в месяц, из которой после стандартных расходов обычно остаётся около 1 200–1 300 € чистой прибыли. "
            "При таких показателях точка выходит на окупаемость в среднем за 9–12 месяцев, но реальный результат всегда зависит от локации и потока людей. "
            "Можем разобрать конкретное место или перейти к условиям сотрудничества."
        ),
        "terms": (
            "Хороший вопрос, это важный момент — и здесь часто бывают неправильные ожидания. "
            "Maison de Café — это не классическая франшиза с жёсткими правилами и паушальными взносами. "
            "Это партнёрская модель: вы инвестируете в оборудование и управляете точкой, а мы обеспечиваем продукт, стандарты качества, "
            "обучение и поддержку на старте. У вас остаётся свобода в выборе локации и управлении бизнесом. "
            "Можем обсудить вашу идею или перейти к следующему шагу."
        ),
        "contacts": (
            "Хороший вопрос. Если вы дошли до этого этапа, значит формат вам действительно интересен. "
            "Самый полезный следующий шаг — коротко обсудить вашу ситуацию: локацию, бюджет и ожидания. "
            "Так становится понятно, насколько Maison de Café подходит именно вам, без теории и лишних обещаний. "
            "Можем либо оформить заявку и разобрать всё персонально, либо вернуться к цифрам и ещё раз спокойно пройтись по окупаемости.\n\n"
            f"{CONTACTS_TEXT['RU']}"
        ),
    }
}

def gold_lang(lang: str) -> str:
    return lang if lang in LANGS else "RU"


def reply_menu(lang: str) -> ReplyKeyboardMarkup:
    L = MENU_LABELS.get(lang, MENU_LABELS["RU"])
    keyboard = [
        [KeyboardButton(L["what"])],
        [KeyboardButton(L["price"])],
        [KeyboardButton(L["payback"])],
        [KeyboardButton(L["terms"])],
        [KeyboardButton(L["contacts"])],
        [KeyboardButton(L["presentation"])],
        [KeyboardButton(L["lang"])],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,  # ВАЖНО: так iOS показывает "квадратик"
        input_field_placeholder={
            "UA": "Напишіть питання…",
            "RU": "Напишите вопрос…",
            "EN": "Type your question…",
            "FR": "Écrivez votre question…",
        }.get(lang, "Напишите вопрос…"),
    )


def lang_inline_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(LANG_LABELS["UA"], callback_data="LANG:UA"),
         InlineKeyboardButton(LANG_LABELS["RU"], callback_data="LANG:RU")],
        [InlineKeyboardButton(LANG_LABELS["EN"], callback_data="LANG:EN"),
         InlineKeyboardButton(LANG_LABELS["FR"], callback_data="LANG:FR")],
    ]
    return InlineKeyboardMarkup(kb)


# =========================
# Guardrails (anti "classic franchise" / banned patterns)
# =========================
BANNED_PATTERNS = [
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
    r"\b1\s*500\s*[–-]\s*2\s*000\b",
    r"\bпаушальн",
    r"\bроялти\b",
    r"\broyalt",
    r"\bfranchise\s+fee",
]
def looks_like_legacy_franchise(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)


async def ensure_thread(user: UserState) -> str:
    if user.thread_id:
        return user.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    user.thread_id = thread.id
    save_state()
    return thread.id


def _draft_instructions(lang: str) -> str:
    if lang == "UA":
        return (
            "Ти — Max, консультант Maison de Café. Відповідай по-людськи, спокійно, впевнено. "
            "Не згадуй бази знань/файли/пошук. "
            "НЕ вигадуй цифри, пакети, роялті, паушальні внески або формати «класичної франшизи». "
            "Якщо для точної відповіді бракує даних — поясни це просто і задай 1 коротке уточнення."
        )
    if lang == "EN":
        return (
            "You are Max, a Maison de Café consultant. Speak naturally and confidently. "
            "Do not mention knowledge bases/files/search. "
            "Do NOT invent numbers, packages, royalties, franchise fees, or generic coffee-shop templates. "
            "If details are needed, explain simply and ask 1 short clarifying question."
        )
    if lang == "FR":
        return (
            "Tu es Max, consultant Maison de Café. Réponds de façon humaine et sûre. "
            "Ne mentionne pas de base de connaissances/fichiers/recherche. "
            "N’invente pas de chiffres, de packs, de royalties ou de « franchise classique ». "
            "Si des détails manquent, explique simplement et pose 1 question courte."
        )
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по-человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или шаблоны «классической франшизы». "
        "Если для точного ответа не хватает данных — объясни это просто и задай 1 короткий уточняющий вопрос."
    )


# =========================
# Deterministic calculator (margin + expenses)
# =========================
def _extract_cups_per_day(text: str) -> Optional[int]:
    """
    Extract cups/day from user message. Accepts up to 200.
    Triggers on context words (чаш/ cups / cups a day).
    """
    t = (text or "").lower()
    if not any(w in t for w in ["чаш", "cup", "cups", "cups/day", "чашек", "порций"]):
        return None
    nums = re.findall(r"\b(\d{1,3})\b", t)
    if not nums:
        return None
    # Heuristic: take first number <=200
    for n in nums:
        v = int(n)
        if 1 <= v <= 200:
            return v
    return None


def calc_profit_message(lang: str, cups_per_day: int) -> str:
    """
    Uses: margin 1.8 €/cup, 30 days/month, expenses 450–600 €/month.
    Returns gross margin & net range.
    """
    margin_per_cup = 1.8
    days = 30
    gross = cups_per_day * days * margin_per_cup
    net_low = gross - 600
    net_high = gross - 450

    # Keep Max-style opening
    if lang == "EN":
        return (
            "Good question — let’s put numbers on it. "
            f"With about {cups_per_day} cups/day and an average margin of 1.8 € per cup, "
            f"the gross margin is roughly {gross:,.0f} € per month. "
            f"With typical monthly costs of 450–600 €, the net result is about {net_low:,.0f}–{net_high:,.0f} € per month. "
            "If you tell me the city/area and the location type, I’ll help you sanity-check the traffic assumptions."
        )
    if lang == "FR":
        return (
            "Bonne question — mettons des chiffres dessus. "
            f"Avec environ {cups_per_day} tasses/jour et une marge moyenne de 1,8 € par tasse, "
            f"la marge brute est d’environ {gross:,.0f} € par mois. "
            f"Avec des coûts mensuels типiques de 450–600 €, le résultat net est d’environ {net_low:,.0f}–{net_high:,.0f} € par mois. "
            "Dites-moi la ville/quartier et le type d’emplacement — et on valide l’hypothèse de trafic."
        )
    if lang == "UA":
        return (
            "Хороший запит — давайте по цифрах. "
            f"За обсягу приблизно {cups_per_day} чашок/день і середньої маржі 1,8 € з чашки, "
            f"валова маржа виходить близько {gross:,.0f} € на місяць. "
            f"За типових витрат 450–600 € на місяць чистий результат — орієнтовно {net_low:,.0f}–{net_high:,.0f} € на місяць. "
            "Скажіть місто/район і тип локації — допоможу тверезо звірити очікування по трафіку."
        )
    # RU
    return (
        "Хороший вопрос — давайте по цифрам. "
        f"При объёме примерно {cups_per_day} чашек в день и средней марже 1,8 € с чашки "
        f"валовая маржа выходит около {gross:,.0f} € в месяц. "
        f"При типичных ежемесячных расходах 450–600 € чистый результат — ориентировочно {net_low:,.0f}–{net_high:,.0f} € в месяц. "
        "Скажи город/район и тип локации — помогу трезво сверить ожидания по трафику."
    )


# =========================
# ANSWER PIPELINE (2-PASS): DRAFT -> VERIFY -> SEND
# =========================
_ALLOWED_NUMBER_PATTERNS = [
    r"\b9\s*800\b",
    r"\b9800\b",
    r"\b1[\.,]8\b",
    r"\b35\b",
    r"\b1\s*900\b",
    r"\b1200\b",
    r"\b1\s*200\b",
    r"\b1300\b",
    r"\b1\s*300\b",
    r"\b9\s*[–-]\s*12\b",
    r"\b450\b",
    r"\b600\b",
    r"\b200\b",
]

def _has_disallowed_numbers(text: str) -> bool:
    if not text:
        return False
    tmp = text
    for p in _ALLOWED_NUMBER_PATTERNS:
        tmp = re.sub(p, "", tmp)
    return bool(re.search(r"\d", tmp))


# =========================
# PATCH 2: KB-only gate helpers
# =========================
def _run_used_file_search(steps) -> bool:
    """
    Returns True if run steps contain tool_calls with type == 'file_search'.
    Compatible with Assistants API steps schema.
    """
    try:
        for st in getattr(steps, "data", []) or []:
            details = getattr(st, "step_details", None)
            # Typical: step_details.type == "tool_calls"
            if details and getattr(details, "type", "") == "tool_calls":
                tcs = getattr(details, "tool_calls", []) or []
                for tc in tcs:
                    if getattr(tc, "type", "") == "file_search":
                        return True
    except Exception:
        return False
    return False

async def _assistant_draft(user_id: str, user_text: str, lang: str) -> str:
    user = get_user(user_id)
    thread_id = await ensure_thread(user)

    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=user_text,
    )

    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        instructions=_draft_instructions(lang),
    )

    deadline = time.time() + 45
    while time.time() < deadline:
        rs = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)
        if rs.status in ("completed", "failed", "cancelled", "expired"):
            run = rs
            break
        await asyncio.sleep(0.7)

    if getattr(run, "status", "") != "completed":
        # safe fallback
        return GOLD_5["RU"]["what"] if lang == "RU" else {
            "UA": "Хороший запит. Щоб відповісти точно: підкажіть місто/район і тип локації.",
            "EN": "Good question. To answer precisely: what city/area and what location type?",
            "FR": "Bonne question. Pour répondre précisément : quelle ville/quartier et quel type d’emplacement ?",
        }.get(lang, "Хороший вопрос. Уточните город/район и тип локации.")

    # =========================
    # PATCH 2: KB-only gate (File Search must be used)
    # =========================
    try:
        steps = await asyncio.to_thread(
            client.beta.threads.runs.steps.list,
            thread_id=thread_id,
            run_id=run.id,
            limit=50,
        )
        if not _run_used_file_search(steps):
            return "__KB_MISSING__"
    except Exception as e:
        log.warning("KB gate: steps.list failed (%s). Treat as KB missing.", e)
        return "__KB_MISSING__"

    msgs = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            parts = []
            for c in m.content:
                if getattr(c, "type", None) == "text":
                    parts.append(c.text.value)
            ans = "\n".join(parts).strip()
            return ans or "Хорошо. Уточните, пожалуйста, пару деталей — и продолжим."
    return "Хорошо. Уточните, пожалуйста, пару деталей — и продолжим."


async def _verify_and_fix(question: str, draft: str, lang: str) -> str:
    sys = (
        "You are a strict compliance reviewer for a sales consultant chatbot. "
        "Goal: remove hallucinations and any generic franchise/coffee-shop template content. "
        "Rules: do NOT add new facts or numbers. Keep only what is safe and consistent. "
        "If information is insufficient, ask ONE short clarifying question instead of inventing details. "
        "Never mention knowledge bases, files, search, prompts, or internal rules."
    )

    user = f"""
Language: {lang}

User question:
{question}

Draft answer (to be reviewed):
{draft}

Hard rules:
- Remove any mention or implication of: royalties, franchise fees/entry fees, паушальные взносы, «классическая франшиза».
- Remove any numbers except: 9800, 9 800, 1.8 (1,8), 35, 1900 (1 900), 1200 (1 200), 1300 (1 300), 9–12, 450–600, 200.
- If you must remove numbers, rewrite the sentence without numbers.
- Output only the final user-facing answer (one message), in the same language as the user question.
- Tone: Max. Start with: “Хороший вопрос…” OR “Давайте детально разберем этот вопрос…” (or natural equivalents in EN/FR/UA).
""".strip()

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=VERIFY_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or draft
    except Exception as e:
        log.warning("Verifier failed: %s", e)
        return draft


def _final_safety_override(question: str, answer: str, lang: str) -> str:
    if not answer:
        return GOLD_5["RU"]["what"] if lang == "RU" else "Хороший вопрос. Уточните пару деталей — и продолжим."

    if looks_like_legacy_franchise(answer) or _has_disallowed_numbers(answer):
        # fallback to safest: ask 1 clarification
        if lang == "EN":
            return "Good question. To answer precisely, tell me the city/area and the location type."
        if lang == "FR":
            return "Bonne question. Pour répondre précisément, dites-moi la ville/quartier et le type d’emplacement."
        if lang == "UA":
            return "Хороший запит. Щоб відповісти точно, підкажіть місто/район і тип локації."
        return "Хороший вопрос. Чтобы ответить точно, скажите город/район и тип локации."
    return answer


def _kb_missing_reply(lang: str) -> str:
    if lang == "EN":
        return "I can’t answer this correctly from the knowledge base. Please choose a menu item or уточните вопрос."
    if lang == "FR":
        return "Je ne peux pas répondre correctement à partir de la base. Choisissez un пункт du menu ou уточните вопрос."
    if lang == "UA":
        return "Я не можу відповісти коректно з бази. Оберіть пункт меню або уточніть запит."
    return "Я не могу ответить корректно по базе. Выберите пункт меню или уточните вопрос."


async def ask_assistant(user_id: str, user_text: str, lang: str) -> str:
    # 0) deterministic calculator override
    cups = _extract_cups_per_day(user_text)
    if cups is not None:
        return calc_profit_message(lang=lang, cups_per_day=cups)

    # 1) KB draft (with KB-only gate)
    draft = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang)

    # KB gate: if File Search wasn't used => NO ANSWER
    if draft == "__KB_MISSING__":
        return _kb_missing_reply(lang)

    # 2) verify/rewrite
    fixed = await _verify_and_fix(question=user_text, draft=draft, lang=lang)

    # 3) final guard
    return _final_safety_override(question=user_text, answer=fixed, lang=lang)


# =========================
# Typing indicator helper
# =========================
async def _typing_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event) -> None:
    try:
        while not stop_event.is_set():
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(3.5)
    except Exception:
        pass


# =========================
# Button text routing
# =========================
def match_menu_action(lang: str, text: str) -> Optional[str]:
    """
    Returns one of: what/price/payback/terms/contacts/presentation/lang
    """
    if not text:
        return None
    t = text.strip()
    L = MENU_LABELS.get(lang, MENU_LABELS["RU"])
    for key in ["what", "price", "payback", "terms", "contacts", "presentation", "lang"]:
        if t == L[key]:
            return key
    return None

# =========================
# COMMANDS / HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    hello = {
        "UA": "Привіт! Я Max, консультант Maison de Café. Оберіть пункт меню — і я підкажу по суті.",
        "RU": "Привет! Я Max, консультант Maison de Café. Выберите пункт меню — и я подскажу по сути.",
        "EN": "Hi! I’m Max, Maison de Café consultant. Choose a menu item and I’ll guide you.",
        "FR": "Bonjour ! Je suis Max, consultant Maison de Café. Choisissez un пункт du menu et je vous guide.",
    }.get(u.lang, "Привет! Я Max.")
    # IMPORTANT: keyboard appears only here (and after language change)
    await update.message.reply_text(hello, reply_markup=reply_menu(u.lang))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if OWNER_TELEGRAM_ID and user_id != OWNER_TELEGRAM_ID:
        return
    await update.message.reply_text(
        f"Users: {len(_state)}\nBlocked: {len(_blocked)}\nAssistant: {ASSISTANT_ID}\nToken: {mask_token(TELEGRAM_BOT_TOKEN)}"
    )


async def on_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = str(q.from_user.id)
    if user_id in _blocked:
        return

    data = q.data or ""
    if not data.startswith("LANG:"):
        return

    lang = data.split(":", 1)[1].strip()
    u = get_user(user_id)
    if lang in LANGS:
        u.lang = lang
        save_state()

    confirm = {"UA": "Мову змінено.", "RU": "Язык изменён.", "EN": "Language updated.", "FR": "Langue mise à jour."}.get(u.lang, "OK")

    # IMPORTANT: show reply keyboard again after language change (per your requirement)
    await q.message.reply_text(confirm, reply_markup=reply_menu(u.lang))


async def send_presentation(chat_id: int, lang: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PRESENTATION_FILE_ID:
        msg = {
            "UA": "Хороший запит. Презентація ще не підключена — додамо файл і я одразу зможу її надіслати.",
            "RU": "Хороший вопрос. Презентация ещё не подключена — добавим файл и я сразу смогу её отправить.",
            "EN": "Good question. The presentation isn’t connected yet — once the file is added, I can send it right away.",
            "FR": "Bonne question. La présentation n’est pas encore connectée — dès que le fichier est ajouté, je peux l’envoyer.",
        }.get(lang, "Презентация ещё не подключена.")
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return

    try:
        await context.bot.send_document(chat_id=chat_id, document=PRESENTATION_FILE_ID)
    except Exception as e:
        log.warning("Presentation send failed: %s", e)
        msg = {
            "UA": "Хороший запит. Не зміг відправити презентацію в цьому чаті. Напишіть — і я надішлю іншим способом.",
            "RU": "Хороший вопрос. Не получилось отправить презентацию в этом чате. Напишите — и я пришлю другим способом.",
            "EN": "Good question. I couldn’t send the presentation here. Message me and I’ll share it another way.",
            "FR": "Bonne question. Je n’arrive pas à envoyer la présentation ici. Écrivez-moi et je la partagerai autrement.",
        }.get(lang, "Не получилось отправить презентацию.")
        await context.bot.send_message(chat_id=chat_id, text=msg)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    text = (update.message.text or "").strip()
    if not text:
        return

    # Per-user lock to avoid double answers/races
    async with get_user_lock(user_id):
        # 1) If pressed one of 7 reply buttons
        action = match_menu_action(u.lang, text)

        if action == "lang":
            # Inline language picker only
            prompt = {"UA": "Оберіть мову:", "RU": "Выберите язык:", "EN": "Choose language:", "FR": "Choisissez la langue:"}.get(u.lang, "Выберите язык:")
            await update.message.reply_text(prompt, reply_markup=lang_inline_keyboard())
            return

        if action == "presentation":
            await send_presentation(chat_id=update.effective_chat.id, lang=u.lang, context=context)
            return

        if action in ("what", "price", "payback", "terms", "contacts"):
            # GOLD responses for buttons
            if u.lang == "RU":
                await update.message.reply_text(GOLD_5["RU"][action])
            else:
                # for non-RU, use assistant pipeline (still safe) but keep Max-start phrasing via verifier.
                stop = asyncio.Event()
                typing_task = asyncio.create_task(_typing_loop(context, update.effective_chat.id, stop))
                try:
                    ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)
                finally:
                    stop.set()
                    await typing_task
                await update.message.reply_text(ans)
            return

        # 2) Free text -> assistant pipeline
        stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(context, update.effective_chat.id, stop))
        try:
            ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)
        finally:
            stop.set()
            await typing_task

        # IMPORTANT: do NOT attach reply keyboard here (so it doesn't feel like "buttons after every answer")
        await update.message.reply_text(ans)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return
    u = get_user(user_id)

    async with get_user_lock(user_id):
        stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(context, update.effective_chat.id, stop))
        try:
            voice = update.message.voice
            if not voice:
                return

            tg_file = await context.bot.get_file(voice.file_id)
            ogg_path = f"/tmp/voice_{user_id}_{int(time.time())}.ogg"
            await tg_file.download_to_drive(ogg_path)

            # Transcribe
            with open(ogg_path, "rb") as f:
                tr = await asyncio.to_thread(
                    client.audio.transcriptions.create,
                    model=TRANSCRIBE_MODEL,
                    file=f,
                )
            transcript = (getattr(tr, "text", "") or "").strip()

            if not transcript:
                msg = {
                    "UA": "Хороший запит. Не зміг розпізнати голос. Спробуйте ще раз коротше й чіткіше.",
                    "RU": "Хороший вопрос. Не смог распознать голос. Попробуйте ещё раз короче и чётче.",
                    "EN": "Good question. I couldn’t transcribe the voice message. Please try again, shorter and clearer.",
                    "FR": "Bonne question. Je n’ai pas pu transcrire le message vocal. Réessayez plus court et plus clair.",
                }.get(u.lang, "Не смог распознать голос.")
                await update.message.reply_text(msg)
                return

            ans = await ask_assistant(user_id=user_id, user_text=transcript, lang=u.lang)
            await update.message.reply_text(ans)
        finally:
            stop.set()
            await typing_task


# Polling anti-conflict: clear webhook + drop pending updates
async def post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared (drop_pending_updates=True)")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)


def build_app() -> Application:
    return ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()


def main() -> None:
    acquire_single_instance_lock()
    load_state()

    app = build_app()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline callbacks only for language picker
    app.add_handler(CallbackQueryHandler(on_lang_callback, pattern=r"^LANG:"))

    # Voice
    app.add_handler(MessageHandler(filters.VOICE, on_voice))

    # Text (non-commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
