import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Optional, Any, Tuple

from dotenv import load_dotenv

import fcntl  # Linux-only (Render OK)

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

def _get_env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n, "") or "").strip()
        if v:
            return v
    return ""

TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")
OPENAI_API_KEY = _get_env("OPENAI_API_KEY")
ASSISTANT_ID = _get_env("ASSISTANT_ID")

OWNER_TELEGRAM_ID = _get_env("OWNER_TELEGRAM_ID")  # optional
PRESENTATION_FILE_ID = _get_env("PRESENTATION_FILE_ID")  # optional: Telegram file_id

VERIFY_MODEL = _get_env("VERIFY_MODEL") or "gpt-4o-mini"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN)")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not ASSISTANT_ID:
    raise RuntimeError("Missing ASSISTANT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mdc_bot")

def mask_token(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 10:
        return tok
    return f"{tok[:4]}…{tok[-6:]}"

log.info("Boot: TELEGRAM token=%s", mask_token(TELEGRAM_BOT_TOKEN))
log.info("Boot: ASSISTANT_ID=%s", ASSISTANT_ID)
log.info("Boot: OWNER_TELEGRAM_ID=%s", OWNER_TELEGRAM_ID or "(not set)")

# =========================
# SINGLE INSTANCE LOCK (Render)
# =========================
_LOCK_PATH = "/tmp/mdc_bot.lock"
_lock_fp = None

def acquire_single_instance_lock() -> None:
    global _lock_fp
    _lock_fp = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
        log.info("Single-instance lock acquired: %s", _LOCK_PATH)
    except BlockingIOError:
        log.error("Another instance is already running (lock busy). Exiting.")
        raise SystemExit(0)

# =========================
# STATE (persisted)
# =========================
STATE_FILE = Path("mdc_state.json")

LANGS = ["UA", "RU", "EN", "FR"]

LANG_LABELS = {
    "UA": "🇺🇦 Українська",
    "RU": "🇷🇺 Русский",
    "EN": "🇬🇧 English",
    "FR": "🇫🇷 Français",
}

# 7 buttons must be ReplyKeyboard (so Telegram shows the “square” icon when collapsed)
MENU_LABELS = {
    "UA": {
        "what": "☕ Що таке Maison de Café?",
        "price": "💶 Скільки коштує відкрити?",
        "payback": "📈 Окупність і прибуток",
        "terms": "🤝 Умови співпраці",
        "contacts": "📞 Контакти / наступний крок",
        "lead": "📝 Залишити заявку",
        "lang": "🌍 Мова / Language",
        "presentation": "📄 Презентація",
    },
    "RU": {
        "what": "☕ Что такое Maison de Café?",
        "price": "💶 Сколько стоит открыть?",
        "payback": "📈 Окупаемость и прибыль",
        "terms": "🤝 Условия сотрудничества",
        "contacts": "📞 Контакты / следующий шаг",
        "lead": "📝 Оставить заявку",
        "lang": "🌍 Язык / Language",
        "presentation": "📄 Презентация",
    },
    "EN": {
        "what": "☕ What is Maison de Café?",
        "price": "💶 Opening cost",
        "payback": "📈 Payback & profit",
        "terms": "🤝 Partnership terms",
        "contacts": "📞 Contacts / next step",
        "lead": "📝 Leave a request",
        "lang": "🌍 Language",
        "presentation": "📄 Presentation",
    },
    "FR": {
        "what": "☕ Qu’est-ce que Maison de Café ?",
        "price": "💶 Coût de lancement",
        "payback": "📈 Rentabilité & profit",
        "terms": "🤝 Conditions",
        "contacts": "📞 Contacts / prochain pas",
        "lead": "📝 Laisser une demande",
        "lang": "🌍 Langue / Language",
        "presentation": "📄 Présentation",
    },
}

CONTACTS_TEXT = {
    "UA": "Контакти Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "RU": "Контакты Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "EN": "Maison de Café contacts:\n• Email: maisondecafe.coffee@gmail.com\n• Phone: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "FR": "Contacts Maison de Café:\n• Email : maisondecafe.coffee@gmail.com\n• Téléphone : +32 470 600 806\n• Telegram : https://t.me/maisondecafe",
}

# =========================
# GOLD (your 5 standards) — RU (exactly as provided)
# =========================
GOLD_RU = {
    "what": (
        "Хороший вопрос, с него обычно и начинается знакомство. Maison de Café — это готовая точка самообслуживания "
        "под ключ в Бельгии. Вы получаете профессиональный кофейный автомат Jetinno JL-300, фирменную стойку, систему "
        "контроля и стартовый набор ингредиентов, а также обучение и сопровождение запуска. Формат рассчитан на быстрый "
        "старт без опыта в кофейном бизнесе и работу без персонала. Дальше логично либо разобрать стоимость запуска, "
        "либо посмотреть на окупаемость и реальные цифры."
    ),
    "price": (
        "Это самый логичный вопрос, и тут важно сразу говорить честно. Базовая стоимость запуска точки Maison de Café в "
        "Бельгии составляет 9 800 €. В эту сумму входит профессиональный автомат Jetinno JL-300, фирменная стойка, "
        "телеметрия, стартовый набор ингредиентов, обучение и полный запуск. Это не франшиза с пакетами и скрытыми "
        "платежами — вы платите за конкретное оборудование и сервис. Отдельно обычно учитываются только вещи, зависящие "
        "от вашей ситуации, например аренда локации или электричество. Дальше логично либо посмотреть окупаемость, "
        "либо обсудить вашу будущую локацию."
    ),
    "payback": (
        "Без понимания цифр действительно нет смысла идти дальше. В базовой модели Maison de Café средняя маржа с одной "
        "чашки составляет около 1,8 €, а типичный объём продаж — примерно 35 чашек в день. Это даёт валовую маржу "
        "порядка 1 900 € в месяц, из которой после стандартных расходов обычно остаётся около 1 200–1 300 € чистой "
        "прибыли. При таких показателях точка выходит на окупаемость в среднем за 9–12 месяцев, но реальный результат "
        "всегда зависит от локации и потока людей. Можем разобрать конкретное место или перейти к условиям сотрудничества."
    ),
    "terms": (
        "Это важный момент, и здесь часто бывают неправильные ожидания. Maison de Café — это не классическая франшиза "
        "с жёсткими правилами и паушальными взносами. Это партнёрская модель: вы инвестируете в оборудование и управляете "
        "точкой, а мы обеспечиваем продукт, стандарты качества, обучение и поддержку на старте. У вас остаётся свобода "
        "в выборе локации и управлении бизнесом. Можем обсудить вашу идею или перейти к следующему шагу."
    ),
    "contacts": (
        "Если вы дошли до этого этапа, значит формат вам действительно интересен. Самый полезный следующий шаг — коротко "
        "обсудить вашу ситуацию: локацию, бюджет и ожидания. Так становится понятно, насколько Maison de Café подходит "
        "именно вам, без теории и лишних обещаний. Можем либо оформить заявку и разобрать всё персонально, либо вернуться "
        "к цифрам и ещё раз спокойно пройтись по окупаемости."
    ),
}

# Minimal UA/EN/FR versions (kept safe; you can later replace with your own standards)
GOLD_OTHER = {
    "UA": {
        "what": "Хороший запит — з цього зазвичай і починається знайомство. Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії: Jetinno JL-300, фірмова стійка, контроль, старт інгредієнтів, навчання та запуск. Далі логічно або розібрати вартість, або перейти до окупності й цифр.",
        "price": "Базова вартість запуску точки Maison de Café в Бельгії — 9 800 €. У цю суму входить Jetinno JL-300, стійка, телеметрія, старт інгредієнтів, навчання та запуск. Окремо зазвичай — оренда локації та електрика. Далі можемо перейти до окупності або обговорити вашу локацію.",
        "payback": "У базовій моделі маржа ≈ 1,8 €/чашка, типовий обсяг ≈ 35 чашок/день. Це дає валову маржу ≈ 1 900 €/міс, і після витрат часто лишається ≈ 1 200–1 300 €. Окупність у середньому 9–12 міс, але вирішує локація.",
        "terms": "Це партнерська модель: ви інвестуєте в обладнання і керуєте точкою, а ми даємо продукт, стандарти, навчання і підтримку. Це не «класична франшиза».",
        "contacts": CONTACTS_TEXT["UA"],
    },
    "EN": {
        "what": "Maison de Café is a turnkey self-service coffee point in Belgium (Jetinno JL-300, branded stand, control, starter ingredients, training and launch). Next we can discuss the opening cost or go straight to payback and numbers.",
        "price": "The base launch cost is 9 800 € (Jetinno JL-300, stand, telemetry, starter set, training and launch). Rent/electricity are usually separate. Next we can review payback or your location.",
        "payback": "Base model: ~1.8 €/cup margin, ~35 cups/day. That’s ~1 900 €/month gross margin and often ~1 200–1 300 € net after typical costs. Payback ~9–12 months, but location traffic is key.",
        "terms": "Partnership model: you invest and manage the point; we provide product, standards, training and launch support. Not a classic franchise.",
        "contacts": CONTACTS_TEXT["EN"],
    },
    "FR": {
        "what": "Maison de Café est un point café en libre-service clé en main en Belgique (Jetinno JL-300, stand, contrôle, kit ingrédients, formation et lancement). Ensuite : coût ou rentabilité.",
        "price": "Le coût de base est 9 800 € (JL-300, stand, télémétrie, kit, formation, lancement). Loyer/électricité sont souvent séparés. Ensuite : rentabilité ou emplacement.",
        "payback": "Base : ~1,8 €/tasse, ~35 tasses/jour ⇒ ~1 900 €/mois de marge brute, souvent ~1 200–1 300 € net après coûts типiques. ROI ~9–12 mois, mais l’emplacement décide.",
        "terms": "Modèle partenaire : vous investissez et gérez; nous fournissons produit, standards, formation et support de lancement. Pas une franchise classique.",
        "contacts": CONTACTS_TEXT["FR"],
    },
}

def gold(lang: str, key: str) -> str:
    if lang == "RU":
        return GOLD_RU.get(key, "")
    return GOLD_OTHER.get(lang, GOLD_OTHER["UA"]).get(key, "")

@dataclass
class LeadState:
    active: bool = False
    step: int = 0
    data: Dict[str, str] = field(default_factory=dict)

@dataclass
class UserState:
    lang: str = "UA"
    thread_id: str = ""
    lead: LeadState = field(default_factory=LeadState)

_state: Dict[str, UserState] = {}

def load_state() -> None:
    global _state
    if not STATE_FILE.exists():
        _state = {}
        return
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    users = raw.get("users", {})
    out: Dict[str, UserState] = {}
    for uid, d in users.items():
        lead_d = d.get("lead", {}) or {}
        lead = LeadState(
            active=bool(lead_d.get("active", False)),
            step=int(lead_d.get("step", 0)),
            data=dict(lead_d.get("data", {}) or {}),
        )
        out[uid] = UserState(
            lang=d.get("lang", "UA"),
            thread_id=d.get("thread_id", ""),
            lead=lead,
        )
    _state = out

def save_state() -> None:
    raw = {
        "users": {uid: {"lang": s.lang, "thread_id": s.thread_id, "lead": asdict(s.lead)} for uid, s in _state.items()}
    }
    STATE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user(user_id: str) -> UserState:
    if user_id not in _state:
        _state[user_id] = UserState()
        save_state()
    return _state[user_id]

def reply_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """
    IMPORTANT:
    - This is ReplyKeyboardMarkup.
    - We send it on /start and after language change only.
    - We NEVER send ReplyKeyboardRemove afterwards.
    Telegram will then keep the keyboard available via the “square” icon when collapsed.
    """
    L = MENU_LABELS.get(lang, MENU_LABELS["UA"])
    kb = [
        [L["what"], L["price"]],
        [L["payback"], L["terms"]],
        [L["contacts"], L["lead"]],
        [L["presentation"], L["lang"]],
    ]
    return ReplyKeyboardMarkup(
        kb,
        resize_keyboard=True,
        one_time_keyboard=False,  # keep available (so “square” icon exists)
        input_field_placeholder="Напишите вопрос…",
    )

def lang_inline_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(LANG_LABELS["UA"], callback_data="lang:UA"),
         InlineKeyboardButton(LANG_LABELS["RU"], callback_data="lang:RU")],
        [InlineKeyboardButton(LANG_LABELS["EN"], callback_data="lang:EN"),
         InlineKeyboardButton(LANG_LABELS["FR"], callback_data="lang:FR")],
    ]
    return InlineKeyboardMarkup(kb)

# =========================
# Calculator (deterministic)
# =========================
MARGIN_PER_CUP = 1.8
DAYS_PER_MONTH = 30
EXP_MIN = 450
EXP_MAX = 600
CUPS_MIN = 1
CUPS_MAX = 200
INVESTMENT = 9800

def _extract_cups(text: str) -> Optional[int]:
    if not text:
        return None
    nums = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text)
    if not nums:
        return None
    # choose the most plausible cups/day number within 1..200
    for n in reversed(nums):
        try:
            v = int(n)
            if CUPS_MIN <= v <= CUPS_MAX:
                return v
        except:
            continue
    return None

def _calc_profit(cups_per_day: int) -> Dict[str, Any]:
    gross_month = MARGIN_PER_CUP * cups_per_day * DAYS_PER_MONTH
    net_min = gross_month - EXP_MAX  # worst case expenses
    net_max = gross_month - EXP_MIN  # best case expenses
    payback_min = None
    payback_max = None
    if net_max > 0:
        payback_min = INVESTMENT / net_max  # fastest payback
    if net_min > 0:
        payback_max = INVESTMENT / net_min  # slowest payback
    return {
        "cups": cups_per_day,
        "gross": gross_month,
        "net_min": net_min,
        "net_max": net_max,
        "payback_min": payback_min,
        "payback_max": payback_max,
    }

def _format_money(x: float) -> str:
    # 1890.0 -> "1 890"
    s = f"{x:,.0f}".replace(",", " ")
    return s

def calculator_answer(lang: str, cups: int) -> str:
    r = _calc_profit(cups)
    gross = _format_money(r["gross"])
    net_min = _format_money(r["net_min"])
    net_max = _format_money(r["net_max"])

    if lang == "RU":
        lines = [
            f"Ок, считаю по модели Maison de Café для {cups} чашек/день:",
            f"• Маржа: 1,8 €/чашка",
            f"• Валовая маржа/мес: 1,8 × {cups} × 30 = {gross} €",
            f"• Средние расходы/мес: {EXP_MIN}–{EXP_MAX} €",
            f"• Ориентир чистыми/мес: {net_min}–{net_max} €",
        ]
        if r["payback_min"] and r["payback_max"]:
            lines.append(f"• Окупаемость (при инвестиции 9 800 €): примерно {r['payback_min']:.1f}–{r['payback_max']:.1f} мес")
        lines.append("Если скажешь тип локации и город/район — помогу оценить реалистичность этих продаж.")
        return "\n".join(lines)

    if lang == "UA":
        lines = [
            f"Ок, рахую для {cups} чашок/день:",
            f"• Маржа: 1,8 €/чашка",
            f"• Валова маржа/міс: 1,8 × {cups} × 30 = {gross} €",
            f"• Витрати/міс: {EXP_MIN}–{EXP_MAX} €",
            f"• Орієнтир чистими/міс: {net_min}–{net_max} €",
            "Скажіть місто/район і тип локації — підкажу, який трафік потрібен під ці продажі."
        ]
        return "\n".join(lines)

    if lang == "EN":
        lines = [
            f"Here’s the model for {cups} cups/day:",
            f"• Margin: 1.8 €/cup",
            f"• Gross margin/month: 1.8 × {cups} × 30 = {gross} €",
            f"• Typical monthly costs: {EXP_MIN}–{EXP_MAX} €",
            f"• Net/month (estimate): {net_min}–{net_max} €",
            "Tell me the city/area and location type and I’ll help you validate these volumes."
        ]
        return "\n".join(lines)

    # FR
    lines = [
        f"Calcul pour {cups} tasses/jour :",
        f"• Marge : 1,8 €/tasse",
        f"• Marge brute/mois : 1,8 × {cups} × 30 = {gross} €",
        f"• Coûts mensuels typiques : {EXP_MIN}–{EXP_MAX} €",
        f"• Net/mois (estimation) : {net_min}–{net_max} €",
        "Dites-moi la ville/quartier et le type d’emplacement — je vous aide à valider ces volumes."
    ]
    return "\n".join(lines)

def should_use_calculator(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.lower()
    # triggers
    trig = any(k in t for k in [
        "сколько я буду", "сколько буду", "сколько заработ", "прибыл", "прибут", "profit", "earn", "how much", "доход",
        "окупаем", "окупн", "payback", "rentab", "маржа", "чаш", "cups"
    ])
    if not trig:
        return None
    cups = _extract_cups(text)
    return cups

# =========================
# Assistant pipeline (KB draft -> verify)
# =========================
BANNED_PATTERNS = [
    r"\bпаушальн",
    r"\bроялти\b",
    r"\bfranchise fee\b",
    r"\bro\w*yal\w*\b",
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
]

def looks_like_legacy_franchise(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)

def _draft_instructions(lang: str) -> str:
    if lang == "UA":
        return (
            "Ти — Max, консультант Maison de Café. Відповідай по суті, без вигадок. "
            "Не згадуй бази знань/файли/пошук. Не вигадуй цифри або «класичну франшизу». "
            "Якщо бракує даних — задай 1 коротке уточнення."
        )
    if lang == "EN":
        return (
            "You are Max, Maison de Café consultant. Be direct and factual. "
            "Do not mention knowledge bases/files/search. Do not invent numbers or classic franchise terms. "
            "If details are missing, ask 1 short clarifying question."
        )
    if lang == "FR":
        return (
            "Tu es Max, consultant Maison de Café. Réponds clairement, sans inventions. "
            "Ne mentionne pas base de connaissances/fichiers/recherche. Pas de franchise classique inventée. "
            "S’il manque des infos, pose 1 question courte."
        )
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по делу, без выдумок. "
        "Не упоминай базы знаний/файлы/поиск. Не придумывай цифры и «классическую франшизу». "
        "Если данных не хватает — задай 1 короткий уточняющий вопрос."
    )

async def ensure_thread(u: UserState) -> str:
    if u.thread_id:
        return u.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    u.thread_id = thread.id
    save_state()
    return thread.id

async def assistant_draft(user_id: str, text: str, lang: str) -> str:
    u = get_user(user_id)
    thread_id = await ensure_thread(u)

    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=text,
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
        return {
            "UA": "Щоб відповісти точніше: яке місто/район і який тип локації ви розглядаєте?",
            "RU": "Чтобы ответить точнее: какой город/район и какой тип локации вы рассматриваете?",
            "EN": "To answer precisely: what city/area and what type of location are you considering?",
            "FR": "Pour répondre précisément : quelle ville/quartier et quel type d’emplacement envisagez-vous ?",
        }.get(lang, "Уточните пару деталей — и продолжим.")

    msgs = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            parts = []
            for c in m.content:
                if getattr(c, "type", None) == "text":
                    parts.append(c.text.value)
            ans = "\n".join(parts).strip()
            return ans or "Уточните пару деталей — и продолжим."
    return "Уточните пару деталей — и продолжим."

async def verify_and_fix(question: str, draft: str, lang: str) -> str:
    # If it smells like legacy franchise, force a safe rewrite.
    sys = (
        "You are a strict reviewer for a sales chatbot. "
        "Remove hallucinations, generic templates, and any franchise-fee/royalty content. "
        "Do not add new facts. If missing info, ask ONE short clarifying question. "
        "Never mention knowledge bases/files/search/internal rules."
    )

    user = f"""
Language: {lang}

User question:
{question}

Draft:
{draft}

Rules:
- Remove franchise-fee/royalty/паушальный/роялти content.
- Do not invent numbers. If you must mention numbers, only use those already present in the draft or question.
- Output only the final user-facing answer in the same language.
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

async def ask_assistant(user_id: str, text: str, lang: str) -> str:
    draft = await assistant_draft(user_id, text, lang)
    if looks_like_legacy_franchise(draft):
        # fall back to safe clarification instead of risking wrong narrative
        return {
            "UA": "Ок. Щоб відповісти без припущень: яке місто/район і який тип локації? Тоді дам відповідь по суті.",
            "RU": "Ок. Чтобы ответить без догадок: какой город/район и какой тип локации? Тогда дам ответ по сути.",
            "EN": "Ok. To answer without assumptions: what city/area and what location type? Then I’ll answer precisely.",
            "FR": "Ok. Pour répondre sans suppositions : quelle ville/quartier et quel type d’emplacement ?",
        }.get(lang, "Уточните детали — и продолжим.")
    return await verify_and_fix(text, draft, lang)

# =========================
# Lead-lite (4 steps)
# =========================
LEAD_PROMPTS = {
    "UA": ["Крок 1/4: Як вас звати?", "Крок 2/4: Телефон (у форматі +32…)?", "Крок 3/4: Ваш email?", "Крок 4/4: Коротко опишіть запит (1–2 речення)."],
    "RU": ["Шаг 1/4: Как вас зовут?", "Шаг 2/4: Телефон (в формате +32…)?", "Шаг 3/4: Ваш email?", "Шаг 4/4: Коротко опишите запрос (1–2 предложения)."],
    "EN": ["Step 1/4: Your name?", "Step 2/4: Phone (e.g., +32…)?", "Step 3/4: Email?", "Step 4/4: Briefly describe your request (1–2 sentences)."],
    "FR": ["Étape 1/4 : Votre nom ?", "Étape 2/4 : Téléphone (+32…)?", "Étape 3/4 : Email ?", "Étape 4/4 : Décrivez brièvement votre demande (1–2 phrases)."],
}

def start_lead(u: UserState) -> str:
    u.lead.active = True
    u.lead.step = 1
    u.lead.data = {}
    save_state()
    return LEAD_PROMPTS.get(u.lang, LEAD_PROMPTS["UA"])[0]

def lead_step_store(u: UserState, text: str) -> Optional[str]:
    # step 1 name, step 2 phone, step 3 email, step 4 message
    if not u.lead.active:
        return None

    step = u.lead.step
    if step == 1:
        u.lead.data["name"] = text
        u.lead.step = 2
        save_state()
        return LEAD_PROMPTS.get(u.lang, LEAD_PROMPTS["UA"])[1]
    if step == 2:
        u.lead.data["phone"] = text
        u.lead.step = 3
        save_state()
        return LEAD_PROMPTS.get(u.lang, LEAD_PROMPTS["UA"])[2]
    if step == 3:
        u.lead.data["email"] = text
        u.lead.step = 4
        save_state()
        return LEAD_PROMPTS.get(u.lang, LEAD_PROMPTS["UA"])[3]
    if step == 4:
        u.lead.data["message"] = text
        u.lead.active = False
        u.lead.step = 0
        save_state()
        return None
    return None

async def send_lead_to_owner(context: ContextTypes.DEFAULT_TYPE, update: Update, u: UserState) -> None:
    if not OWNER_TELEGRAM_ID:
        return
    try:
        user = update.effective_user
        chat = update.effective_chat
        payload = (
            f"New lead (Maison de Café)\n"
            f"Telegram user_id: {user.id}\n"
            f"Username: @{user.username}\n"
            f"Chat id: {chat.id}\n"
            f"Name: {u.lead.data.get('name','')}\n"
            f"Phone: {u.lead.data.get('phone','')}\n"
            f"Email: {u.lead.data.get('email','')}\n"
            f"Message: {u.lead.data.get('message','')}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await context.bot.send_message(chat_id=int(OWNER_TELEGRAM_ID), text=payload)
    except Exception as e:
        log.warning("Failed to send lead to owner: %s", e)

# =========================
# Voice -> Transcribe
# =========================
async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    try:
        voice = update.message.voice
        if not voice:
            return None
        file = await context.bot.get_file(voice.file_id)
        # download to temp
        tmp_path = Path(f"/tmp/voice_{update.effective_user.id}_{int(time.time())}.ogg")
        await file.download_to_drive(custom_path=str(tmp_path))

        with tmp_path.open("rb") as f:
            tr = await asyncio.to_thread(
                client.audio.transcriptions.create,
                model="whisper-1",
                file=f,
            )
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        text = (getattr(tr, "text", "") or "").strip()
        return text or None
    except Exception as e:
        log.warning("Voice transcription failed: %s", e)
        return None

# =========================
# UI HELPERS
# =========================
def is_menu_text(lang: str, text: str) -> Optional[str]:
    L = MENU_LABELS.get(lang, MENU_LABELS["UA"])
    mapping = {
        L["what"]: "what",
        L["price"]: "price",
        L["payback"]: "payback",
        L["terms"]: "terms",
        L["contacts"]: "contacts",
        L["lead"]: "lead",
        L["lang"]: "lang",
        L["presentation"]: "presentation",
    }
    return mapping.get(text)

# =========================
# HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    txt = {
        "UA": "Привіт! Я Max, консультант Maison de Café.\nОберіть пункт меню або просто напишіть питання — я відповім по суті.",
        "RU": "Привет! Я Макс, консультант Maison de Café.\nВыберите пункт меню или просто напишите вопрос — я отвечу по сути.",
        "EN": "Hi! I’m Max, Maison de Café consultant.\nChoose a menu item or type your question — I’ll answer to the point.",
        "FR": "Bonjour ! Je suis Max, consultant Maison de Café.\nChoisissez un пункт du menu ou écrivez votre question — je réponds clairement.",
    }.get(u.lang, "Hi!")

    # IMPORTANT: show Reply keyboard here (creates the “square” icon when collapsed later)
    await update.message.reply_text(
        txt,
        reply_markup=reply_menu_keyboard(u.lang),
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_TELEGRAM_ID and str(update.effective_user.id) != str(OWNER_TELEGRAM_ID):
        return
    await update.message.reply_text(
        f"Users: {len(_state)}\nAssistant: {ASSISTANT_ID}\nToken: {mask_token(TELEGRAM_BOT_TOKEN)}\nPresentation: {'set' if PRESENTATION_FILE_ID else 'not set'}"
    )

async def on_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = str(q.from_user.id)
    u = get_user(user_id)
    data = q.data or ""

    if not data.startswith("lang:"):
        return
    lang = data.split(":", 1)[1]
    if lang not in LANGS:
        return

    u.lang = lang
    save_state()

    # IMPORTANT: after language change we re-send ReplyKeyboardMarkup (so labels update)
    await q.message.reply_text(
        {"UA": "Мову змінено.", "RU": "Язык изменён.", "EN": "Language updated.", "FR": "Langue mise à jour."}.get(u.lang, "OK"),
        reply_markup=reply_menu_keyboard(u.lang),
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)
    text = (update.message.text or "").strip()
    if not text:
        return

    # If lead flow is active, consume steps first
    if u.lead.active:
        nxt = lead_step_store(u, text)
        if nxt is None:
            # lead completed
            await send_lead_to_owner(context, update, u)
            done = {
                "UA": "Дякуємо! Заявку відправлено. Наш менеджер зв’яжеться з вами найближчим часом.",
                "RU": "Спасибо! Заявка отправлена. Наш менеджер свяжется с вами в ближайшее время.",
                "EN": "Thanks! Your request has been sent. Our manager will contact you shortly.",
                "FR": "Merci ! Votre demande a été envoyée. Notre manager vous contactera bientôt.",
            }.get(u.lang, "OK")
            # IMPORTANT: DO NOT remove keyboard
            await update.message.reply_text(done)
        else:
            await update.message.reply_text(nxt)
        return

    # Menu button pressed?
    menu_key = is_menu_text(u.lang, text)
    if menu_key:
        # IMPORTANT: DO NOT attach keyboard again; DO NOT remove it.
        if menu_key in ("what", "price", "payback", "terms", "contacts"):
            await update.message.reply_text(gold(u.lang, menu_key))
            return

        if menu_key == "lang":
            await update.message.reply_text(
                {"UA": "Оберіть мову:", "RU": "Выберите язык:", "EN": "Choose language:", "FR": "Choisissez la langue:"}.get(u.lang, "Choose language:"),
                reply_markup=lang_inline_keyboard(),
            )
            return

        if menu_key == "presentation":
            if PRESENTATION_FILE_ID:
                try:
                    await context.bot.send_document(chat_id=update.effective_chat.id, document=PRESENTATION_FILE_ID)
                except Exception as e:
                    log.warning("Presentation send failed: %s", e)
                    await update.message.reply_text(
                        {"UA": "Не зміг відправити презентацію. Напишіть мені — і я надішлю її іншим способом.",
                         "RU": "Не получилось отправить презентацию. Напишите мне — и я пришлю другим способом.",
                         "EN": "I couldn't send the presentation here. Message me and I’ll share it another way.",
                         "FR": "Je n’arrive pas à envoyer la présentation ici. Écrivez-moi et je la partagerai autrement."}.get(u.lang, "Couldn't send.")
                    )
            else:
                await update.message.reply_text(
                    {"UA": "Презентація ще не підключена. Додамо файл — і я одразу зможу її надсилати.",
                     "RU": "Презентация ещё не подключена. Добавим файл — и я сразу смогу её отправлять.",
                     "EN": "Presentation is not connected yet. Once the file is added, I’ll be able to send it.",
                     "FR": "La présentation n’est pas encore connectée. Dès que le fichier est ajouté, je pourrai l’envoyer."}.get(u.lang, "Not connected yet.")
                )
            return

        if menu_key == "lead":
            prompt = start_lead(u)
            await update.message.reply_text(prompt)
            return

    # Calculator (deterministic) for “how much will I earn with X cups”
    cups = should_use_calculator(text)
    if cups is not None:
        await update.message.reply_text(calculator_answer(u.lang, cups))
        return

    # Normal free-text -> assistant pipeline
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    ans = await ask_assistant(user_id, text, u.lang)
    await update.message.reply_text(ans)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    text = await transcribe_voice(update, context)
    if not text:
        await update.message.reply_text(
            {"UA": "Не зміг розпізнати голос. Спробуйте ще раз або напишіть текстом.",
             "RU": "Не смог распознать голос. Попробуйте ещё раз или напишите текстом.",
             "EN": "I couldn't transcribe the voice message. Please try again or type your question.",
             "FR": "Je n’ai pas pu transcrire le message vocal. Réessayez ou écrivez votre question."}.get(u.lang, "Try again.")
        )
        return

    # If user said menu-like thing in voice (rare), just treat as normal question
    cups = should_use_calculator(text)
    if cups is not None:
        await update.message.reply_text(calculator_answer(u.lang, cups))
        return

    ans = await ask_assistant(user_id, text, u.lang)
    await update.message.reply_text(ans)

# Polling anti-conflict: clear webhook to avoid telegram.error.Conflict
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

    app.add_handler(CallbackQueryHandler(on_language_callback, pattern=r"^lang:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot started (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
