# =========================
# bot.py (PART 1/2)
# Maison de Café — Max bot
# =========================

import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

from dotenv import load_dotenv

import fcntl  # Linux-only; OK on Render

from telegram import (
    Update,
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

# Support both names to avoid "missing token" regressions
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
ASSISTANT_ID = (os.getenv("ASSISTANT_ID") or "").strip()

OWNER_TELEGRAM_ID = (os.getenv("OWNER_TELEGRAM_ID") or "").strip()
PRESENTATION_FILE_ID = (os.getenv("PRESENTATION_FILE_ID") or "").strip()  # Telegram file_id for PDF
VERIFY_MODEL = (os.getenv("VERIFY_MODEL") or "gpt-4o-mini").strip()

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
log.info("Boot: VERIFY_MODEL=%s", VERIFY_MODEL)


# =========================
# SINGLE INSTANCE LOCK (Variant B)
# =========================
LOCK_PATH = "/tmp/mdc_bot.lock"
_lock_fd = None


def acquire_single_instance_lock() -> None:
    """
    Prevent two processes from polling simultaneously (telegram.error.Conflict).
    Variant B: OS file lock. If lock is held -> exit immediately.
    """
    global _lock_fd
    _lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        log.info("Single-instance lock acquired: %s", LOCK_PATH)
    except BlockingIOError:
        log.error("Another instance is already running. Exiting.")
        raise SystemExit(0)


# =========================
# STATE (persisted)
# =========================
STATE_FILE = Path("mdc_state.json")


@dataclass
class UserState:
    lang: str = "RU"          # UA / RU / EN / FR
    thread_id: str = ""       # OpenAI thread per user (stable)
    lead_step: int = 0        # 0=off, 1..n steps
    lead_data: Dict[str, Any] = None

    def __post_init__(self):
        if self.lead_data is None:
            self.lead_data = {}


_state: Dict[str, UserState] = {}
_blocked: set = set()


def load_state() -> None:
    global _state, _blocked
    if not STATE_FILE.exists():
        _state = {}
        _blocked = set()
        return

    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    _blocked = set(raw.get("blocked", []))
    users = raw.get("users", {})
    _state = {}
    for uid, data in users.items():
        _state[uid] = UserState(
            lang=data.get("lang", "RU"),
            thread_id=data.get("thread_id", ""),
            lead_step=data.get("lead_step", 0),
            lead_data=data.get("lead_data", {}) or {},
        )


def save_state() -> None:
    raw = {
        "blocked": sorted(list(_blocked)),
        "users": {
            uid: {
                "lang": s.lang,
                "thread_id": s.thread_id,
                "lead_step": s.lead_step,
                "lead_data": s.lead_data,
            }
            for uid, s in _state.items()
        },
    }
    STATE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user(user_id: str) -> UserState:
    if user_id not in _state:
        _state[user_id] = UserState()
        save_state()
    return _state[user_id]


# =========================
# LANG / LABELS
# =========================
LANGS = ["UA", "RU", "EN", "FR"]

LANG_LABELS = {
    "UA": "🇺🇦 Українська",
    "RU": "🇷🇺 Русский",
    "EN": "🇬🇧 English",
    "FR": "🇫🇷 Français",
}

CONTACTS_TEXT = {
    "UA": "Контакти Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "RU": "Контакты Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "EN": "Maison de Café contacts:\n• Email: maisondecafe.coffee@gmail.com\n• Phone: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "FR": "Contacts Maison de Café:\n• Email : maisondecafe.coffee@gmail.com\n• Téléphone : +32 470 600 806\n• Telegram : https://t.me/maisondecafe",
}

# =========================
# GOLD STANDARD (5 answers) — EXACT CORE MEANING
# =========================
GOLD_5 = {
    "RU": {
        "what": (
            "Хороший вопрос, с него обычно и начинается знакомство. Maison de Café — это готовая точка самообслуживания под ключ в Бельгии. "
            "Вы получаете профессиональный кофейный автомат Jetinno JL-300, фирменную стойку, систему контроля и стартовый набор ингредиентов, "
            "а также обучение и сопровождение запуска. Формат рассчитан на быстрый старт без опыта в кофейном бизнесе и работу без персонала. "
            "Дальше логично либо разобрать стоимость запуска, либо посмотреть на окупаемость и реальные цифры."
        ),
        "price": (
            "Это самый логичный вопрос, и тут важно сразу говорить честно. Базовая стоимость запуска точки Maison de Café в Бельгии составляет 9 800 €. "
            "В эту сумму входит профессиональный автомат Jetinno JL-300, фирменная стойка, телеметрия, стартовый набор ингредиентов, обучение и полный запуск. "
            "Это не франшиза с пакетами и скрытыми платежами — вы платите за конкретное оборудование и сервис. "
            "Отдельно обычно учитываются только вещи, зависящие от вашей ситуации, например аренда локации или электричество. "
            "Дальше логично либо посмотреть окупаемость, либо обсудить вашу будущую локацию."
        ),
        "payback": (
            "Без понимания цифр действительно нет смысла идти дальше. В базовой модели Maison de Café средняя маржа с одной чашки составляет около 1,8 €, "
            "а типичный объём продаж — примерно 35 чашек в день. Это даёт валовую маржу порядка 1 900 € в месяц, "
            "из которой после стандартных расходов обычно остаётся около 1 200–1 300 € чистой прибыли. "
            "При таких показателях точка выходит на окупаемость в среднем за 9–12 месяцев, но реальный результат всегда зависит от локации и потока людей. "
            "Можем разобрать конкретное место или перейти к условиям сотрудничества."
        ),
        "terms": (
            "Это важный момент, и здесь часто бывают неправильные ожидания. Maison de Café — это не классическая франшиза с жёсткими правилами и паушальными взносами. "
            "Это партнёрская модель: вы инвестируете в оборудование и управляете точкой, а мы обеспечиваем продукт, стандарты качества, обучение и поддержку на старте. "
            "У вас остаётся свобода в выборе локации и управлении бизнесом. Можем обсудить вашу идею или перейти к следующему шагу."
        ),
        "contacts_next": (
            "Если вы дошли до этого этапа, значит формат вам действительно интересен. Самый полезный следующий шаг — коротко обсудить вашу ситуацию: "
            "локацию, бюджет и ожидания. Так становится понятно, насколько Maison de Café подходит именно вам, без теории и лишних обещаний. "
            "Можем либо оформить заявку и разобрать всё персонально, либо вернуться к цифрам и ещё раз спокойно пройтись по окупаемости."
        ),
    },
    # For UA/EN/FR we keep consistent meaning, not adding new facts.
    "UA": {
        "what": (
            "Хороший запит — з нього зазвичай і починається знайомство. Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії. "
            "Ви отримуєте професійний автомат Jetinno JL-300, фірмову стійку, систему контролю та стартовий набір інгредієнтів, "
            "а також навчання і супровід запуску. Формат розрахований на швидкий старт без досвіду та роботу без персоналу. "
            "Далі логічно або розібрати вартість запуску, або перейти до окупності й реальних цифр."
        ),
        "price": (
            "Це найлогічніше питання — і тут важливо одразу говорити чесно. Базова вартість запуску точки Maison de Café в Бельгії — 9 800 €. "
            "У суму входить Jetinno JL-300, фірмова стійка, телеметрія, стартовий набір інгредієнтів, навчання та повний запуск. "
            "Це не франшиза з пакетами та прихованими платежами — ви платите за конкретне обладнання та сервіс. "
            "Окремо зазвичай враховуються лише речі, що залежать від вашої ситуації, наприклад оренда локації або електрика. "
            "Далі логічно або подивитися окупність, або обговорити вашу майбутню локацію."
        ),
        "payback": (
            "Без розуміння цифр немає сенсу йти далі. У базовій моделі Maison de Café середня маржа з чашки — близько 1,8 €, "
            "а типовий обсяг — приблизно 35 чашок на день. Це дає валову маржу близько 1 900 € на місяць, "
            "з якої після стандартних витрат зазвичай лишається близько 1 200–1 300 € чистого прибутку. "
            "Окупність у середньому — 9–12 місяців, але реальний результат залежить від локації та потоку людей. "
            "Можемо розібрати конкретне місце або перейти до умов співпраці."
        ),
        "terms": (
            "Важливий момент — тут часто бувають неправильні очікування. Maison de Café — це не класична франшиза з жорсткими правилами та паушальними внесками. "
            "Це партнерська модель: ви інвестуєте в обладнання та керуєте точкою, а ми забезпечуємо продукт, стандарти якості, навчання і підтримку на старті. "
            "У вас залишається свобода у виборі локації та управлінні бізнесом. Можемо обговорити вашу ідею або перейти до наступного кроку."
        ),
        "contacts_next": (
            "Якщо ви дійшли до цього етапу, значить формат вам справді цікавий. Найкорисніший наступний крок — коротко обговорити вашу ситуацію: "
            "локацію, бюджет і очікування. Так стає зрозуміло, чи підходить Maison de Café саме вам — без теорії та зайвих обіцянок. "
            "Можемо або оформити заявку і розібрати все персонально, або повернутися до цифр і ще раз спокійно пройтися по окупності."
        ),
    },
    "EN": {
        "what": (
            "Good question — that’s usually where the conversation starts. Maison de Café is a turnkey self-service point in Belgium. "
            "You get a professional Jetinno JL-300 machine, a branded stand, a control system and a starter set of ingredients, plus training and launch support. "
            "It’s designed for a fast start without prior coffee-business experience and works without staff. "
            "Next, it makes sense to either discuss the launch cost or move straight to payback and real numbers."
        ),
        "price": (
            "This is the most logical question, and it’s important to be upfront. The base cost to launch a Maison de Café point in Belgium is 9 800 €. "
            "It includes the Jetinno JL-300, branded stand, telemetry, a starter set of ingredients, training and a full launch. "
            "This is not a classic franchise with packages and hidden fees — you pay for specific equipment and service. "
            "Separate costs usually depend on your situation (for example, rent or electricity). "
            "Next, we can either look at payback or discuss your target location."
        ),
        "payback": (
            "Without numbers, there’s no point moving forward. In the base Maison de Café model, the average margin per cup is about 1.8 €, "
            "and a typical volume is around 35 cups/day. That gives roughly 1 900 € gross margin per month, "
            "and after standard monthly costs it often leaves around 1 200–1 300 € net profit. "
            "Payback is typically 9–12 months, but the real result depends on the location and traffic. "
            "We can review a specific spot or move to partnership terms."
        ),
        "terms": (
            "This is an important point — expectations are often wrong here. Maison de Café is not a classic franchise with strict rules and entry fees. "
            "It’s a partnership model: you invest in equipment and manage the point, while we provide product, quality standards, training and launch support. "
            "You keep freedom in choosing the location and managing the business. We can discuss your idea or move to the next step."
        ),
        "contacts_next": (
            "If you’ve reached this stage, the format is clearly interesting to you. The most useful next step is to briefly discuss your situation: "
            "location, budget and expectations. That makes it clear whether Maison de Café fits you — without theory or empty promises. "
            "We can either submit a request and go through it personally, or return to the numbers and calmly review payback again."
        ),
    },
    "FR": {
        "what": (
            "Bonne question — c’est souvent ainsi que l’échange commence. Maison de Café est un point en libre-service « clé en main » en Belgique. "
            "Vous recevez une machine professionnelle Jetinno JL-300, un stand de marque, un système de contrôle et un kit de démarrage d’ingrédients, "
            "ainsi que la formation et l’accompagnement au lancement. Le format permet de démarrer vite sans expérience et fonctionne sans personnel. "
            "Ensuite, il est logique soit de voir le coût de lancement, soit de passer à la rentabilité et aux chiffres."
        ),
        "price": (
            "C’est la question la plus logique, et il faut être transparent. Le coût de base pour lancer un point Maison de Café en Belgique est de 9 800 €. "
            "Cela inclut la Jetinno JL-300, le stand, la télémétrie, le kit d’ingrédients, la formation et le lancement complet. "
            "Ce n’est pas une franchise classique avec packs et frais cachés — vous payez pour un équipement et un service concrets. "
            "Les coûts séparés dépendent généralement de votre situation (par exemple loyer ou électricité). "
            "Ensuite, on peut soit regarder la rentabilité, soit discuter de votre futur emplacement."
        ),
        "payback": (
            "Sans chiffres, cela n’a pas de sens d’aller plus loin. Dans le modèle de base Maison de Café, la marge moyenne par tasse est d’environ 1,8 €, "
            "et le volume типique est d’environ 35 tasses/jour. Cela donne environ 1 900 € de marge brute par mois, "
            "et après les coûts mensuels standard il reste souvent autour de 1 200–1 300 € de bénéfice net. "
            "Le retour sur investissement est en général de 9–12 mois, mais le résultat réel dépend de l’emplacement et du flux. "
            "On peut analyser un lieu concret ou passer aux conditions de partenariat."
        ),
        "terms": (
            "Point important — les attentes sont souvent incorrectes ici. Maison de Café n’est pas une franchise classique avec règles strictes et droits d’entrée. "
            "C’est un modèle partenaire : vous investissez dans l’équipement et vous gérez le point, et nous fournissons le produit, les standards qualité, "
            "la formation et l’accompagnement au lancement. Vous gardez la liberté de choisir l’emplacement et de gérer le business. "
            "On peut discuter de votre idée ou passer à l’étape suivante."
        ),
        "contacts_next": (
            "Si vous en êtes arrivé là, c’est que le format vous intéresse vraiment. La prochaine étape la plus utile est de discuter brièvement de votre situation : "
            "emplacement, budget et attentes. Cela permet de savoir si Maison de Café vous convient — sans théorie ni promesses vides. "
            "On peut soit laisser une demande et tout revoir персонно, soit revenir aux chiffres et refaire calmement la rentabilité."
        ),
    },
}


def norm_lang(lang: str) -> str:
    return lang if lang in LANGS else "RU"


# =========================
# MENU (7 reply-buttons) + inline language
# =========================
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

# Build reverse map from button text -> key
def build_reverse_menu_map() -> Dict[str, str]:
    m: Dict[str, str] = {}
    for lang, labels in MENU_LABELS.items():
        for key, txt in labels.items():
            m[txt] = key
    return m


REVERSE_MENU_MAP = build_reverse_menu_map()


def reply_menu(lang: str):
    """
    ReplyKeyboardMarkup must be sent only on /start and after language switch.
    IMPORTANT: We do NOT send ReplyKeyboardRemove() later.
    Using one_time_keyboard=True gives the "square" to bring it back.
    """
    from telegram import ReplyKeyboardMarkup  # local import to keep top clean

    L = MENU_LABELS.get(lang, MENU_LABELS["RU"])
    # 7 buttons total
    keyboard = [
        [L["what"]],
        [L["price"]],
        [L["payback"]],
        [L["terms"]],
        [L["contacts"]],
        [L["presentation"]],
        [L["lang"]],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,   # key UX: hides after tap, but keeps square to reopen
        selective=False,
        is_persistent=False,
    )


def inline_lang_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(LANG_LABELS["UA"], callback_data="l:UA"),
            InlineKeyboardButton(LANG_LABELS["RU"], callback_data="l:RU"),
        ],
        [
            InlineKeyboardButton(LANG_LABELS["EN"], callback_data="l:EN"),
            InlineKeyboardButton(LANG_LABELS["FR"], callback_data="l:FR"),
        ],
    ]
    return InlineKeyboardMarkup(kb)


# =========================
# Anti-hallucination guards (LLM)
# =========================
BANNED_PATTERNS = [
    r"\bроялти\b",
    r"\bпаушальн",
    r"\bfranchise fee\b",
    r"\broyalt",
    r"\bentry fee\b",
    r"\bпакет\b",
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
]

def looks_like_legacy_franchise(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)


# =========================
# Calculator (deterministic)
# =========================
MARGIN_PER_CUP = 1.8
MAX_CUPS_PER_DAY = 200
MONTH_DAYS = 30
EXPENSES_MIN = 450
EXPENSES_MAX = 600

def parse_cups_per_day(text: str) -> Optional[int]:
    """
    Extract cups/day from user text.
    Accepts: '35 чашек', '40 cups', '50 в день', 'до 200', etc.
    """
    if not text:
        return None
    t = text.lower()

    # If user writes: "35 чашек" or "35 cups"
    m = re.search(r"\b(\d{1,3})\b\s*(чаш|cup|cups)\b", t)
    if m:
        n = int(m.group(1))
        return n

    # If user writes: "35 в день" / "35 в сутки"
    m = re.search(r"\b(\d{1,3})\b\s*(в\s*(день|сутки)|per\s*day|/day)\b", t)
    if m:
        n = int(m.group(1))
        return n

    # If user asks "если 35" in profit context:
    m = re.search(r"\b(\d{1,3})\b", t)
    if m:
        n = int(m.group(1))
        # Keep it only if it looks like a cups question
        if any(w in t for w in ["чаш", "cups", "в день", "per day", "зараб", "прибыл", "прибут", "profit", "rentab", "сколько буду"]):
            return n

    return None


def is_profit_question(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(w in t for w in [
        "сколько буду", "сколько я буду", "сколько заработ", "прибыл", "прибыль",
        "прибут", "profit", "rentab", "rentabilité", "окуп", "окупа",
        "модель", "бизнес-модель", "business model"
    ])


def calc_profit_message(lang: str, cups_per_day: int) -> str:
    lang = norm_lang(lang)
    cups = max(0, min(int(cups_per_day), MAX_CUPS_PER_DAY))

    gross_month = cups * MARGIN_PER_CUP * MONTH_DAYS
    net_min = gross_month - EXPENSES_MAX
    net_max = gross_month - EXPENSES_MIN

    # Friendly formatting
    def eur(x: float) -> str:
        return f"{int(round(x)):,}".replace(",", " ")

    if lang == "UA":
        return (
            f"Ок, порахую по вашій цифрі.\n"
            f"• Чашок/день: {cups}\n"
            f"• Середня маржа/чашка: {MARGIN_PER_CUP} €\n"
            f"• Валова маржа/місяць (≈{MONTH_DAYS} днів): ~{eur(gross_month)} €\n"
            f"• Орієнтовно «чистими» після витрат {EXPENSES_MIN}–{EXPENSES_MAX} €/міс: ~{eur(net_min)}–{eur(net_max)} €\n\n"
            f"Хочете — скажіть тип локації (лікарня/ТЦ/бізнес-центр) і я підкажу, який обсяг чашок реалістичний саме там."
        )
    if lang == "EN":
        return (
            f"OK — let’s calculate with your number.\n"
            f"• Cups/day: {cups}\n"
            f"• Avg margin/cup: {MARGIN_PER_CUP} €\n"
            f"• Gross margin/month (≈{MONTH_DAYS} days): ~{eur(gross_month)} €\n"
            f"• Estimated net after {EXPENSES_MIN}–{EXPENSES_MAX} €/month costs: ~{eur(net_min)}–{eur(net_max)} €\n\n"
            f"Tell me the location type (hospital / mall / business center) and I’ll comment what cup volume is realistic there."
        )
    if lang == "FR":
        return (
            f"OK — je calcule avec votre chiffre.\n"
            f"• Tasses/jour : {cups}\n"
            f"• Marge moyenne/tasse : {MARGIN_PER_CUP} €\n"
            f"• Marge brute/mois (≈{MONTH_DAYS} jours) : ~{eur(gross_month)} €\n"
            f"• Estimation net après {EXPENSES_MIN}–{EXPENSES_MAX} €/mois de coûts : ~{eur(net_min)}–{eur(net_max)} €\n\n"
            f"Dites-moi le type d’emplacement (hôpital / centre commercial / business center) et je vous dirai quel volume est réaliste."
        )
    # RU default
    return (
        f"Ок, считаю по вашей цифре.\n"
        f"• Чашек/день: {cups}\n"
        f"• Средняя маржа/чашка: {MARGIN_PER_CUP} €\n"
        f"• Валовая маржа/месяц (≈{MONTH_DAYS} дней): ~{eur(gross_month)} €\n"
        f"• Ориентировочно «чистыми» после расходов {EXPENSES_MIN}–{EXPENSES_MAX} €/мес: ~{eur(net_min)}–{eur(net_max)} €\n\n"
        f"Хотите — скажите тип локации (больница/ТЦ/бизнес-центр), и я подскажу, какой объём чашек реально ожидать именно там."
    )


# =========================
# Assistant thread handling
# =========================
async def ensure_thread(u: UserState) -> str:
    if u.thread_id:
        return u.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    u.thread_id = thread.id
    save_state()
    return thread.id


def draft_instructions(lang: str) -> str:
    lang = norm_lang(lang)
    if lang == "UA":
        return (
            "Ти — Max, консультант Maison de Café. Відповідай по-людськи, спокійно, впевнено. "
            "Не згадуй бази знань/файли/пошук. "
            "НЕ вигадуй цифри, пакети, роялті, паушальні внески або «класичну франшизу». "
            "Якщо даних бракує — задай 1 коротке уточнення."
        )
    if lang == "EN":
        return (
            "You are Max, a Maison de Café consultant. Speak naturally and confidently. "
            "Do not mention knowledge bases/files/search. "
            "Do NOT invent numbers, packages, royalties, or classic franchise templates. "
            "If details are missing, ask 1 short clarifying question."
        )
    if lang == "FR":
        return (
            "Tu es Max, consultant Maison de Café. Réponds de façon humaine et sûre. "
            "Ne mentionne pas base de connaissances/fichiers/recherche. "
            "N’invente pas de chiffres, packs, royalties ou franchise classique. "
            "S’il manque des détails, pose 1 question courte."
        )
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по-человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или «классическую франшизу». "
        "Если данных не хватает — задай 1 короткий уточняющий вопрос."
    )


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
        instructions=draft_instructions(lang),
    )

    deadline = time.time() + 45
    while time.time() < deadline:
        rs = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)
        if rs.status in ("completed", "failed", "cancelled", "expired"):
            run = rs
            break
        await asyncio.sleep(0.7)

    if getattr(run, "status", "") != "completed":
        # deterministic fallback without inventing numbers
        lang = norm_lang(lang)
        return {
            "UA": "Щоб відповісти точно, підкажіть: яка локація (місто/район) і який формат місця ви розглядаєте?",
            "RU": "Чтобы ответить точно, подскажите: какая локация (город/район) и какой формат места рассматриваете?",
            "EN": "To answer precisely: what city/area and what type of location are you considering?",
            "FR": "Pour répondre précisément : quelle ville/quartier et quel type d’emplacement envisagez-vous ?",
        }[lang]

    msgs = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            parts = []
            for c in m.content:
                if getattr(c, "type", None) == "text":
                    parts.append(c.text.value)
            ans = "\n".join(parts).strip()
            return ans or "Ок. Уточните, пожалуйста, пару деталей — и продолжим."
    return "Ок. Уточните, пожалуйста, пару деталей — и продолжим."


async def verify_and_fix(question: str, draft: str, lang: str) -> str:
    """
    PASS 2: remove hallucinations / forbidden franchise content.
    We also prevent random numbers (except allowed small set).
    """
    lang = norm_lang(lang)

    allowed_number_patterns = [
        r"\b9\s*800\b", r"\b9800\b",
        r"\b1[\.,]8\b",
        r"\b35\b",
        r"\b1\s*900\b", r"\b1900\b",
        r"\b1\s*200\b", r"\b1200\b",
        r"\b1\s*300\b", r"\b1300\b",
        r"\b9\s*[–-]\s*12\b",
        r"\b450\b", r"\b600\b",  # expenses range is allowed only because WE own it deterministically
        r"\b200\b",              # cap is allowed
        r"\b30\b",               # month days
    ]

    def has_disallowed_numbers(text: str) -> bool:
        if not text:
            return False
        tmp = text
        for p in allowed_number_patterns:
            tmp = re.sub(p, "", tmp)
        return bool(re.search(r"\d", tmp))

    if looks_like_legacy_franchise(draft) or has_disallowed_numbers(draft):
        # Use verifier model to rewrite safely
        sys = (
            "You are a strict compliance reviewer for a sales chatbot. "
            "Remove hallucinations and forbidden franchise content. "
            "Do NOT add new facts or new numbers. "
            "If info is insufficient, ask ONE short clarifying question. "
            "Never mention knowledge bases/files/search/internal rules."
        )

        user_msg = f"""
Language: {lang}

User question:
{question}

Draft answer:
{draft}

Hard rules:
- Remove any mention/implication of royalties, franchise fees, entry fees, packages, classic franchise templates.
- Remove any numbers except: 9800/9 800, 1.8/1,8, 35, 1900/1 900, 1200/1 200, 1300/1 300, 9–12, 450–600, 30, 200.
- If you remove a number, rewrite the sentence without numbers.
- Output only the final answer in the same language as the question, Max tone, with a clear next step.
""".strip()

        try:
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=VERIFY_MODEL,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user_msg},
                ],
            )
            out = (resp.choices[0].message.content or "").strip()
            return out or draft
        except Exception as e:
            log.warning("Verifier failed: %s", e)
            return draft

    return draft


async def ask_assistant(user_id: str, text: str, lang: str) -> str:
    """
    Main pipeline for free text:
    - If profit question + cups => deterministic calculator
    - else assistant draft + verifier
    """
    lang = norm_lang(lang)
    if is_profit_question(text):
        cups = parse_cups_per_day(text)
        if cups is None:
            if lang == "UA":
                return "Ок. Скажіть, будь ласка, скільки чашок на день ви плануєте (наприклад 30 / 40 / 50)?"
            if lang == "EN":
                return "OK. How many cups per day are you targeting (for example 30 / 40 / 50)?"
            if lang == "FR":
                return "OK. Combien de tasses par jour visez-vous (par exemple 30 / 40 / 50) ?"
            return "Ок. Скажите, пожалуйста, сколько чашек в день вы планируете (например 30 / 40 / 50)?"
        return calc_profit_message(lang, cups)

    draft = await assistant_draft(user_id=user_id, text=text, lang=lang)
    fixed = await verify_and_fix(question=text, draft=draft, lang=lang)
    if looks_like_legacy_franchise(fixed):
        # safe fallback: ask 1 clarifying question
        if lang == "UA":
            return "Щоб відповісти точно, підкажіть: яка локація (тип місця) і ваше місто/район?"
        if lang == "EN":
            return "To answer precisely: what type of location and what city/area?"
        if lang == "FR":
            return "Pour répondre précisément : quel type d’emplacement et quelle ville/quartier ?"
        return "Чтобы ответить точно, подскажите: тип локации и город/район?"
    return fixed

# =========================
# bot.py (PART 2/2)
# =========================

# =========================
# Voice transcription (OpenAI)
# =========================
async def transcribe_voice_to_text(file_path: str) -> str:
    """
    Uses OpenAI audio transcription.
    Works with OGG/OPUS typically sent by Telegram.
    """
    try:
        with open(file_path, "rb") as f:
            tr = await asyncio.to_thread(
                client.audio.transcriptions.create,
                model="whisper-1",
                file=f,
            )
        return (getattr(tr, "text", "") or "").strip()
    except Exception as e:
        log.warning("Transcription failed: %s", e)
        return ""


# =========================
# Commands / Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    lang = norm_lang(u.lang)
    if lang == "UA":
        txt = "Привіт! Я Max, консультант Maison de Café. Оберіть пункт меню або просто напишіть питання."
    elif lang == "EN":
        txt = "Hi! I’m Max, Maison de Café consultant. Choose a menu item or just type your question."
    elif lang == "FR":
        txt = "Bonjour ! Je suis Max, consultant Maison de Café. Choisissez un пункт du menu ou écrivez votre question."
    else:
        txt = "Привет! Я Max, консультант Maison de Café. Выберите пункт меню или напишите вопрос."

    # IMPORTANT: reply keyboard shown ONLY here
    await update.message.reply_text(txt, reply_markup=reply_menu(lang))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if OWNER_TELEGRAM_ID and user_id != OWNER_TELEGRAM_ID:
        return
    await update.message.reply_text(
        "STATUS\n"
        f"Users: {len(_state)}\n"
        f"Blocked: {len(_blocked)}\n"
        f"Assistant: {ASSISTANT_ID}\n"
        f"Token: {mask_token(TELEGRAM_BOT_TOKEN)}\n"
        f"Presentation file_id set: {'yes' if bool(PRESENTATION_FILE_ID) else 'no'}"
    )


async def on_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = str(q.from_user.id)
    if user_id in _blocked:
        return
    u = get_user(user_id)

    data = q.data or ""
    if not data.startswith("l:"):
        return
    lang = data.split(":", 1)[1].strip()
    if lang not in LANGS:
        return

    u.lang = lang
    save_state()

    # IMPORTANT: after language change — show reply keyboard again (UX requirement)
    if lang == "UA":
        msg = "Мову змінено."
    elif lang == "EN":
        msg = "Language updated."
    elif lang == "FR":
        msg = "Langue mise à jour."
    else:
        msg = "Язык изменён."

    # send message + menu (ONLY here)
    await q.message.reply_text(msg, reply_markup=reply_menu(lang))


async def send_presentation(chat_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    lang = norm_lang(lang)
    if not PRESENTATION_FILE_ID:
        if lang == "UA":
            await context.bot.send_message(chat_id=chat_id, text="Презентація ще не підключена. Я можу надіслати її, як тільки додамо файл.")
        elif lang == "EN":
            await context.bot.send_message(chat_id=chat_id, text="The presentation is not connected yet. I can send it as soon as we add the file.")
        elif lang == "FR":
            await context.bot.send_message(chat_id=chat_id, text="La présentation n’est pas encore connectée. Je peux l’envoyer dès que le fichier est ajouté.")
        else:
            await context.bot.send_message(chat_id=chat_id, text="Презентация ещё не подключена. Могу отправить, как только добавим файл.")
        return

    try:
        await context.bot.send_document(chat_id=chat_id, document=PRESENTATION_FILE_ID)
    except Exception as e:
        log.warning("Presentation send failed: %s", e)
        if lang == "UA":
            await context.bot.send_message(chat_id=chat_id, text="Не зміг відправити презентацію тут. Напишіть — надішлю іншим способом.")
        elif lang == "EN":
            await context.bot.send_message(chat_id=chat_id, text="I couldn't send the file here. Message me and I’ll share it another way.")
        elif lang == "FR":
            await context.bot.send_message(chat_id=chat_id, text="Je n’arrive pas à envoyer le fichier ici. Écrivez-moi et je le partagerai autrement.")
        else:
            await context.bot.send_message(chat_id=chat_id, text="Не получилось отправить файл тут. Напишите — пришлю другим способом.")


def gold(lang: str, key: str) -> str:
    lang = norm_lang(lang)
    return GOLD_5.get(lang, GOLD_5["RU"]).get(key, GOLD_5["RU"]["what"])


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, key: str) -> bool:
    """
    Handles reply-menu buttons.
    Returns True if handled.
    """
    u = get_user(user_id)
    lang = norm_lang(u.lang)
    chat_id = update.effective_chat.id

    # IMPORTANT: do NOT attach reply keyboard here (UX requirement)
    if key == "lang":
        # show inline language picker
        if lang == "UA":
            await update.message.reply_text("Оберіть мову:", reply_markup=inline_lang_keyboard())
        elif lang == "EN":
            await update.message.reply_text("Choose language:", reply_markup=inline_lang_keyboard())
        elif lang == "FR":
            await update.message.reply_text("Choisissez la langue:", reply_markup=inline_lang_keyboard())
        else:
            await update.message.reply_text("Выберите язык:", reply_markup=inline_lang_keyboard())
        return True

    if key == "presentation":
        await send_presentation(chat_id=chat_id, context=context, lang=lang)
        return True

    if key == "contacts":
        # contacts + next step (GOLD #5 core) + contacts block
        await update.message.reply_text(gold(lang, "contacts_next"))
        await update.message.reply_text(CONTACTS_TEXT.get(lang, CONTACTS_TEXT["RU"]))
        return True

    if key == "what":
        await update.message.reply_text(gold(lang, "what"))
        return True

    if key == "price":
        await update.message.reply_text(gold(lang, "price"))
        return True

    if key == "payback":
        # GOLD payback + also allow quick calc suggestion (without inventing)
        await update.message.reply_text(gold(lang, "payback"))
        if lang == "UA":
            await update.message.reply_text("Якщо хочете — напишіть вашу ціль по чашках/день (наприклад 30 / 40 / 50), і я порахую модель.")
        elif lang == "EN":
            await update.message.reply_text("If you want, tell me your target cups/day (e.g., 30 / 40 / 50) and I’ll calculate the model.")
        elif lang == "FR":
            await update.message.reply_text("Si vous voulez, donnez votre objectif en tasses/jour (ex. 30 / 40 / 50) et je calcule le modèle.")
        else:
            await update.message.reply_text("Если хотите — напишите вашу цель по чашкам/день (например 30 / 40 / 50), и я посчитаю модель.")
        return True

    if key == "terms":
        await update.message.reply_text(gold(lang, "terms"))
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    lang = norm_lang(u.lang)
    text = (update.message.text or "").strip()
    if not text:
        return

    # 1) If user tapped a reply-menu button -> handle deterministically
    key = REVERSE_MENU_MAP.get(text)
    if key:
        handled = await handle_menu_button(update, context, user_id, key)
        if handled:
            return

    # 2) Free text -> assistant / calculator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    ans = await ask_assistant(user_id=user_id, text=text, lang=lang)
    await update.message.reply_text(ans)  # IMPORTANT: no ReplyKeyboardRemove()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    lang = norm_lang(u.lang)

    voice = update.message.voice
    if not voice:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Download voice
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        tmp_path = f"/tmp/voice_{user_id}_{int(time.time())}.ogg"
        await tg_file.download_to_drive(custom_path=tmp_path)
    except Exception as e:
        log.warning("Voice download failed: %s", e)
        if lang == "UA":
            await update.message.reply_text("Не зміг отримати голосове. Спробуйте ще раз або напишіть текстом.")
        elif lang == "EN":
            await update.message.reply_text("I couldn't download the voice message. Please try again or send text.")
        elif lang == "FR":
            await update.message.reply_text("Je n’ai pas pu récupérer le vocal. Réessayez ou envoyez du texte.")
        else:
            await update.message.reply_text("Не смог получить голосовое. Попробуйте ещё раз или напишите текстом.")
        return

    # Transcribe
    text = await transcribe_voice_to_text(tmp_path)
    try:
        os.remove(tmp_path)
    except Exception:
        pass

    if not text:
        if lang == "UA":
            await update.message.reply_text("Не розпізнав голос. Спробуйте ще раз або напишіть текстом.")
        elif lang == "EN":
            await update.message.reply_text("I couldn’t understand the audio. Please try again or send text.")
        elif lang == "FR":
            await update.message.reply_text("Je n’ai pas compris l’audio. Réessayez ou envoyez du texte.")
        else:
            await update.message.reply_text("Не распознал голос. Попробуйте ещё раз или напишите текстом.")
        return

    # Same pipeline
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    ans = await ask_assistant(user_id=user_id, text=text, lang=lang)
    await update.message.reply_text(ans)


# =========================
# Polling safety (avoid webhook conflicts)
# =========================
async def post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared (drop_pending_updates=True)")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)


def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline language callbacks
    app.add_handler(CallbackQueryHandler(on_lang_callback, pattern=r"^l:(UA|RU|EN|FR)$"))

    # Voice first (so it doesn't fall into text handler)
    app.add_handler(MessageHandler(filters.VOICE, on_voice))

    # Text (non-command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main() -> None:
    acquire_single_instance_lock()
    load_state()
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
