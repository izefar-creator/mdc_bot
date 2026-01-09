import os
import re
import json
import time
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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


# ==========================================================
# ENV
# ==========================================================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "").strip()

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID", "").strip()

# Telegram file_id for presentation PDF (one file for all languages)
PRESENTATION_FILE_ID = os.getenv("PRESENTATION_FILE_ID", "").strip()

# Verifier model (2nd pass). No KB access.
VERIFY_MODEL = os.getenv("VERIFY_MODEL", "gpt-4o-mini").strip()

# STT model for voice
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe").strip()

# Instance lock file (Variant B)
INSTANCE_LOCK_FILE = os.getenv("INSTANCE_LOCK_FILE", "healthbot_instance.lock").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID missing")

client = OpenAI(api_key=OPENAI_API_KEY)


# ==========================================================
# LOGGING
# ==========================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("healthbot")


def mask_token(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 10:
        return tok
    return f"{tok[:4]}…{tok[-6:]}"


log.info("Boot: TELEGRAM token=%s", mask_token(TELEGRAM_BOT_TOKEN))
log.info("Boot: ASSISTANT_ID=%s", ASSISTANT_ID)


# ==========================================================
# SINGLE-INSTANCE LOCK (Variant B)
# If another process is running, this one exits immediately.
# ==========================================================
_lock_handle = None


def acquire_single_instance_lock_or_exit() -> None:
    """
    Variant B: file lock. If cannot lock -> exit.
    Render/Linux supports fcntl. Fallback to exclusive create.
    """
    global _lock_handle
    lock_path = Path(INSTANCE_LOCK_FILE).resolve()

    # Ensure directory exists
    if lock_path.parent and not lock_path.parent.exists():
        lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl  # Linux/Unix
        _lock_handle = open(lock_path, "w")
        try:
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another instance is already running (lock busy).")
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        log.info("Instance lock acquired: %s", str(lock_path))
        return
    except ImportError:
        # Fallback: exclusive create
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            _lock_handle = os.fdopen(fd, "w")
            _lock_handle.write(str(os.getpid()))
            _lock_handle.flush()
            log.info("Instance lock acquired (fallback): %s", str(lock_path))
            return
        except FileExistsError:
            raise RuntimeError("Another instance is already running (lock file exists).")


# ==========================================================
# STATE (persisted)
# ==========================================================
STATE_FILE = Path("healthbot_state.json")


@dataclass
class UserState:
    lang: str = "RU"       # UA/RU/EN/FR
    thread_id: str = ""    # per-user thread


_state: Dict[str, UserState] = {}
_blocked = set()


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


LANGS = ["UA", "RU", "EN", "FR"]

LANG_LABELS = {
    "UA": "🇺🇦 Українська",
    "RU": "🇷🇺 Русский",
    "EN": "🇬🇧 English",
    "FR": "🇫🇷 Français",
}

# Reply buttons labels per language (7 buttons)
MENU_LABELS = {
    "UA": {
        "what": "☕ Що таке Maison de Café?",
        "price": "💶 Скільки коштує відкрити?",
        "payback": "📈 Окупність і прибуток",
        "terms": "🤝 Умови співпраці",
        "contacts": "📞 Контакти / наступний крок",
        "lead": "📝 Залишити заявку",
        "lang": "🌍 Мова",
        "presentation": "📄 Презентація",
    },
    "RU": {
        "what": "☕ Что такое Maison de Café?",
        "price": "💶 Сколько стоит открыть?",
        "payback": "📈 Окупаемость и прибыль",
        "terms": "🤝 Условия сотрудничества",
        "contacts": "📞 Контакты / следующий шаг",
        "lead": "📝 Оставить заявку",
        "lang": "🌍 Язык",
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
        "contacts": "📞 Contacts / prochaine étape",
        "lead": "📝 Laisser une demande",
        "lang": "🌍 Langue",
        "presentation": "📄 Présentation",
    },
}

CONTACTS_TEXT = {
    "UA": "Контакти Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "RU": "Контакты Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "EN": "Maison de Café contacts:\n• Email: maisondecafe.coffee@gmail.com\n• Phone: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "FR": "Contacts Maison de Café:\n• Email : maisondecafe.coffee@gmail.com\n• Téléphone : +32 470 600 806\n• Telegram : https://t.me/maisondecafe",
}

# ==========================================================
# GOLD STANDARD (5 answers) — use for menu buttons
# RU version is EXACT from user; other languages are consistent translations.
# ==========================================================
GOLD = {
    "RU": {
        "what": (
            "Это хороший вопрос, с него обычно и начинается знакомство. "
            "Maison de Café — это готовая точка самообслуживания под ключ в Бельгии. "
            "Вы получаете профессиональный кофейный автомат Jetinno JL-300, фирменную стойку, систему контроля и стартовый набор ингредиентов, "
            "а также обучение и сопровождение запуска. "
            "Формат рассчитан на быстрый старт без опыта в кофейном бизнесе и работу без персонала. "
            "Дальше логично либо разобрать стоимость запуска, либо посмотреть на окупаемость и реальные цифры."
        ),
        "price": (
            "Это хороший вопрос, давайте детально разберем этот вопрос. "
            "Базовая стоимость запуска точки Maison de Café в Бельгии составляет 9 800 €. "
            "В эту сумму входит профессиональный автомат Jetinno JL-300, фирменная стойка, телеметрия, стартовый набор ингредиентов, обучение и полный запуск. "
            "Это не франшиза с пакетами и скрытыми платежами — вы платите за конкретное оборудование и сервис. "
            "Отдельно обычно учитываются только вещи, зависящие от вашей ситуации, например аренда локации или электричество. "
            "Дальше логично либо посмотреть окупаемость, либо обсудить вашу будущую локацию."
        ),
        "payback": (
            "Это хороший вопрос, давайте детально разберем этот вопрос. "
            "В базовой модели Maison de Café средняя маржа с одной чашки составляет около 1,8 €, а типичный объём продаж — примерно 35 чашек в день. "
            "Это даёт валовую маржу порядка 1 900 € в месяц, из которой после стандартных расходов обычно остаётся около 1 200–1 300 € чистой прибыли. "
            "При таких показателях точка выходит на окупаемость в среднем за 9–12 месяцев, но реальный результат всегда зависит от локации и потока людей. "
            "Можем разобрать конкретное место или перейти к условиям сотрудничества."
        ),
        "terms": (
            "Это хороший вопрос, давайте детально разберем этот вопрос. "
            "Maison de Café — это не классическая франшиза с жёсткими правилами и паушальными взносами. "
            "Это партнёрская модель: вы инвестируете в оборудование и управляете точкой, а мы обеспечиваем продукт, стандарты качества, обучение и поддержку на старте. "
            "У вас остаётся свобода в выборе локации и управлении бизнесом. "
            "Можем обсудить вашу идею или перейти к следующему шагу."
        ),
        "contacts": (
            "Это хороший вопрос, давайте детально разберем этот вопрос. "
            "Если вы дошли до этого этапа, значит формат вам действительно интересен. "
            "Самый полезный следующий шаг — коротко обсудить вашу ситуацию: локацию, бюджет и ожидания. "
            "Так становится понятно, насколько Maison de Café подходит именно вам, без теории и лишних обещаний. "
            "Можем либо оформить заявку и разобрать всё персонально, либо вернуться к цифрам и ещё раз спокойно пройтись по окупаемости."
        ),
    },
    "UA": {
        "what": (
            "Це хороший запит — з нього зазвичай і починається знайомство. "
            "Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії. "
            "Ви отримуєте професійний автомат Jetinno JL-300, фірмову стійку, систему контролю та стартовий набір інгредієнтів, "
            "а також навчання і супровід запуску. "
            "Формат розрахований на швидкий старт без досвіду та роботу без персоналу. "
            "Далі логічно або розібрати вартість запуску, або перейти до окупності та цифр."
        ),
        "price": (
            "Це хороший запит, давайте детально розберемо це питання. "
            "Базова вартість запуску точки Maison de Café в Бельгії — 9 800 €. "
            "У цю суму входить Jetinno JL-300, фірмова стійка, телеметрія, стартовий набір інгредієнтів, навчання та повний запуск. "
            "Це не класична франшиза з пакетами та прихованими платежами — ви платите за конкретне обладнання і сервіс. "
            "Окремо зазвичай лишаються тільки витрати, що залежать від вашої ситуації, наприклад оренда локації або електрика. "
            "Далі логічно або подивитися окупність, або обговорити вашу майбутню локацію."
        ),
        "payback": (
            "Це хороший запит, давайте детально розберемо це питання. "
            "У базовій моделі Maison de Café середня маржа з чашки — близько 1,8 €, а типовий обсяг — приблизно 35 чашок на день. "
            "Це дає валову маржу близько 1 900 € на місяць, з якої після стандартних витрат часто лишається близько 1 200–1 300 € чистого результату. "
            "У середньому окупність виходить близько 9–12 місяців, але реальний результат залежить від локації і потоку людей. "
            "Можемо розібрати конкретне місце або перейти до умов співпраці."
        ),
        "terms": (
            "Це хороший запит, давайте детально розберемо це питання. "
            "Maison de Café — це не класична франшиза з паушальними внесками та жорсткими правилами. "
            "Це партнерська модель: ви інвестуєте в обладнання і керуєте точкою, а ми забезпечуємо продукт, стандарти якості, навчання і підтримку на старті. "
            "У вас залишається свобода у виборі локації та управлінні бізнесом. "
            "Можемо обговорити вашу ідею або перейти до наступного кроку."
        ),
        "contacts": (
            "Це хороший запит, давайте детально розберемо це питання. "
            "Якщо ви дійшли до цього етапу — значить формат вам справді цікавий. "
            "Найкорисніший наступний крок — коротко обговорити вашу ситуацію: локацію, бюджет і очікування. "
            "Так стає зрозуміло, чи підходить Maison de Café саме вам, без теорії і зайвих обіцянок. "
            "Можемо або залишити заявку і розібрати все персонально, або повернутися до цифр і спокійно пройтися по окупності."
        ),
    },
    "EN": {
        "what": (
            "That’s a good question — it’s usually how the conversation starts. "
            "Maison de Café is a turnkey self-service coffee point in Belgium. "
            "You get a Jetinno JL-300 machine, a branded stand, a control system, and a starter set of ingredients, "
            "plus training and launch support. "
            "It’s designed for a fast start without prior coffee-business experience and works without staff. "
            "Next, it makes sense to discuss either the opening cost or payback and real numbers."
        ),
        "price": (
            "That’s a good question — let’s break it down clearly. "
            "The base launch cost for a Maison de Café point in Belgium is 9 800 €. "
            "It includes the Jetinno JL-300, branded stand, telemetry, starter ingredients, training, and full launch support. "
            "This is not a classic franchise with packages or hidden fees — you pay for specific equipment and service. "
            "Separate costs usually depend only on your situation, such as rent or electricity. "
            "Next, we can look at payback or discuss your future location."
        ),
        "payback": (
            "That’s a good question — let’s break it down clearly. "
            "In the base model, the average margin per cup is about 1.8 €, and a typical volume is around 35 cups/day. "
            "That’s roughly 1 900 € gross margin per month, and after standard costs it often leaves around 1 200–1 300 € net. "
            "Average payback is about 9–12 months, but the real result always depends on location traffic. "
            "We can assess a specific location or move to partnership terms."
        ),
        "terms": (
            "That’s a good question — let’s break it down clearly. "
            "Maison de Café is not a classic franchise with entry fees and rigid rules. "
            "It’s a partnership model: you invest in the equipment and manage the point, and we provide product, quality standards, training, and launch support. "
            "You keep flexibility in choosing the location and running the business. "
            "We can discuss your idea or move to the next step."
        ),
        "contacts": (
            "That’s a good question — let’s break it down clearly. "
            "If you reached this point, the format is genuinely interesting for you. "
            "The most useful next step is a short talk about your situation: location, budget, and expectations. "
            "That’s how we confirm fit without theory or empty promises. "
            "We can either submit a request and go personal, or return to the numbers and calmly review payback again."
        ),
    },
    "FR": {
        "what": (
            "C’est une bonne question — c’est souvent comme ça que la discussion commence. "
            "Maison de Café est un point café en libre-service « clé en main » en Belgique. "
            "Vous recevez une machine Jetinno JL-300, un stand de marque, un système de contrôle et un kit de démarrage d’ingrédients, "
            "ainsi que la formation et l’accompagnement au lancement. "
            "Le format est pensé pour démarrer vite, sans expérience, et fonctionner sans personnel. "
            "Ensuite, il est logique de parler soit du coût de lancement, soit de la rentabilité et des chiffres."
        ),
        "price": (
            "C’est une bonne question — regardons-la clairement. "
            "Le coût de base pour lancer un point Maison de Café en Belgique est de 9 800 €. "
            "Cela inclut la Jetinno JL-300, le stand, la télémétrie, le kit d’ingrédients, la formation et le lancement. "
            "Ce n’est pas une franchise classique avec packs et frais cachés — vous payez pour un équipement et un service précis. "
            "Les coûts séparés dépendent généralement de votre situation (loyer, électricité). "
            "Ensuite, on peut regarder la rentabilité ou discuter de votre futur emplacement."
        ),
        "payback": (
            "C’est une bonne question — regardons-la clairement. "
            "Dans le modèle de base, la marge moyenne par tasse est d’environ 1,8 €, avec un volume типique d’environ 35 tasses/jour. "
            "Cela donne environ 1 900 € de marge brute par mois, et après les coûts standards il reste souvent autour de 1 200–1 300 € net. "
            "Le retour sur investissement est en moyenne de 9–12 mois, mais le résultat dépend du flux de l’emplacement. "
            "On peut analyser un lieu précis ou passer aux conditions de partenariat."
        ),
        "terms": (
            "C’est une bonne question — regardons-la clairement. "
            "Maison de Café n’est pas une franchise classique avec droits d’entrée et règles rigides. "
            "C’est un modèle partenaire : vous investissez dans l’équipement et gérez le point, et nous fournissons le produit, les standards qualité, la formation et l’accompagnement. "
            "Vous gardez de la flexibilité sur l’emplacement et la gestion. "
            "On peut discuter de votre idée ou passer à la prochaine étape."
        ),
        "contacts": (
            "C’est une bonne question — regardons-la clairement. "
            "Si vous êtes arrivé à ce stade, c’est que le format vous intéresse réellement. "
            "La prochaine étape la plus utile est d’échanger brièvement sur votre situation : emplacement, budget et attentes. "
            "C’est comme ça qu’on valide l’adéquation sans théorie ni promesses inutiles. "
            "On peut soit laisser une demande, soit revenir aux chiffres et revoir la rentabilité calmement."
        ),
    },
}


def gold_lang(lang: str) -> str:
    return lang if lang in GOLD else "RU"


# ==========================================================
# KEYBOARDS
# ==========================================================
def reply_menu(lang: str) -> ReplyKeyboardMarkup:
    L = MENU_LABELS.get(lang, MENU_LABELS["RU"])
    # 7 buttons (include presentation)
    keyboard = [
        [L["what"]],
        [L["price"]],
        [L["payback"]],
        [L["terms"]],
        [L["presentation"]],
        [L["contacts"]],
        [L["lead"], L["lang"]],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,   # Telegram usually hides after press (we also remove explicitly)
        input_field_placeholder="Напишите вопрос или выберите пункт меню…",
    )


def lang_inline_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(LANG_LABELS["UA"], callback_data="l:UA"),
         InlineKeyboardButton(LANG_LABELS["RU"], callback_data="l:RU")],
        [InlineKeyboardButton(LANG_LABELS["EN"], callback_data="l:EN"),
         InlineKeyboardButton(LANG_LABELS["FR"], callback_data="l:FR")],
    ]
    return InlineKeyboardMarkup(kb)


# ==========================================================
# SANITY GUARDS
# ==========================================================
BANNED_PATTERNS = [
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
    r"\b1\s*500\s*[–-]\s*2\s*000\b",
    r"\bпаушальн",
    r"\bроялти\b",
]


def looks_like_legacy_franchise(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)


# ==========================================================
# THREAD
# ==========================================================
async def ensure_thread(user: UserState) -> str:
    if user.thread_id:
        return user.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    user.thread_id = thread.id
    save_state()
    return thread.id


# ==========================================================
# DRAFT INSTRUCTIONS (for Assistant run)
# ==========================================================
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
    # RU
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по-человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или шаблоны «классической франшизы». "
        "Если для точного ответа не хватает данных — объясни это просто и задай 1 короткий уточняющий вопрос."
    )


# ==========================================================
# PROFIT CALCULATOR (deterministic)
# 1.8 * cups/day * 30 - expenses (450..600)
# cups: 1..200
# ==========================================================
def _parse_cups_per_day(text: str) -> Optional[int]:
    """
    Extract cups/day from user text if present.
    Accepts: "30 чашек", "40 cups", "50/day", "50 в день", etc.
    Chooses the most plausible number 1..200.
    """
    if not text:
        return None
    t = text.lower()

    # explicit patterns
    m = re.search(r"(\d{1,3})\s*(?:чаш|cups|cup)\b", t)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 200 else None

    m = re.search(r"(\d{1,3})\s*(?:в\s*день|/day|per\s*day|на\s*день)\b", t)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 200 else None

    # fallback: any number in range 1..200, but avoid years like 2024
    nums = [int(x) for x in re.findall(r"\b(\d{1,3})\b", t)]
    nums = [n for n in nums if 1 <= n <= 200]
    if not nums:
        return None
    # Prefer numbers close to typical ranges (10..120)
    nums.sort(key=lambda x: (0 if 10 <= x <= 120 else 1, abs(x - 35)))
    return nums[0]


def _looks_like_profit_question(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keys = [
        "сколько заработ", "сколько я буду", "сколько получ", "прибыл", "прибут",
        "profit", "earn", "income", "rentab", "окупить", "окуп", "model",
        "бизнес-модель", "сколько в месяц", "сколько в месяц", "gross", "net"
    ]
    return any(k in t for k in keys)


def _profit_answer(lang: str, cups: Optional[int]) -> str:
    gl = gold_lang(lang)

    if cups is None:
        # One short question, but still helpful
        if gl == "UA":
            return (
                "Це хороший запит, давайте детально розберемо це питання. "
                "Скільки чашок на день ви плануєте продавати (від 1 до 200)? "
                "Я порахую модель: 1,8 € маржа × чашки × 30 днів мінус середні витрати 450–600 €."
            )
        if gl == "EN":
            return (
                "That’s a good question — let’s calculate it properly. "
                "How many cups per day do you expect (1 to 200)? "
                "I’ll calculate: 1.8 € margin × cups × 30 days minus typical monthly costs of 450–600 €."
            )
        if gl == "FR":
            return (
                "Bonne question — calculons ça proprement. "
                "Combien de tasses par jour visez-vous (1 à 200) ? "
                "Je calcule : marge 1,8 € × tasses × 30 jours moins les coûts mensuels moyens 450–600 €."
            )
        return (
            "Это хороший вопрос, давайте детально разберем этот вопрос. "
            "Сколько чашек в день вы планируете продавать (от 1 до 200)? "
            "Я посчитаю модель: 1,8 € маржа × чашки × 30 дней минус средние расходы 450–600 €."
        )

    margin_per_cup = 1.8
    days = 30
    gross = margin_per_cup * cups * days
    net_low_cost = gross - 600
    net_high_cost = gross - 450

    # Format amounts without overcomplicating
    def euro(x: float) -> str:
        return f"{x:,.0f} €".replace(",", " ")

    if gl == "UA":
        return (
            "Це хороший запит, давайте детально розберемо це питання. "
            f"Якщо продавати {cups} чашок на день: 1,8 € × {cups} × 30 = приблизно {euro(gross)} валової маржі на місяць. "
            f"Після середніх витрат 450–600 € залишається орієнтовно {euro(net_low_cost)}–{euro(net_high_cost)} на місяць. "
            "Якщо хочете — скажіть локацію (місто/район) і я підкажу, на що звернути увагу, щоб ці цифри були реалістичними."
        )
    if gl == "EN":
        return (
            "That’s a good question — let’s calculate it properly. "
            f"If you sell {cups} cups/day: 1.8 € × {cups} × 30 ≈ {euro(gross)} gross margin/month. "
            f"After typical costs of 450–600 €, you’re at roughly {euro(net_low_cost)}–{euro(net_high_cost)} per month. "
            "If you share the city/area and location type, I’ll help you sanity-check the assumptions."
        )
    if gl == "FR":
        return (
            "Bonne question — calculons ça proprement. "
            f"À {cups} tasses/jour : 1,8 € × {cups} × 30 ≈ {euro(gross)} de marge brute/mois. "
            f"Après des coûts moyens de 450–600 €, il reste environ {euro(net_low_cost)}–{euro(net_high_cost)} par mois. "
            "Si vous me dites la ville/quartier et le type d’emplacement, je vous aide à valider ces hypothèses."
        )
    return (
        "Это хороший вопрос, давайте детально разберем этот вопрос. "
        f"Если продавать {cups} чашек в день: 1,8 € × {cups} × 30 = примерно {euro(gross)} валовой маржи в месяц. "
        f"После средних расходов 450–600 € остаётся ориентировочно {euro(net_low_cost)}–{euro(net_high_cost)} в месяц. "
        "Если хотите — скажите город/район и тип локации, и я помогу проверить реалистичность этих цифр."
    )


# ==========================================================
# ANSWER PIPELINE (2-PASS)
# PASS 1: Assistant + KB
# PASS 2: Verifier rewrite (no KB)
# ==========================================================
# For Assistant answers, keep the “no random numbers” guard,
# but we allow calculator outputs by bypassing assistant.

_ALLOWED_NUMBER_PATTERNS = [
    r"\b9\s*800\b", r"\b9800\b",
    r"\b1[\.,]8\b",
    r"\b35\b",
    r"\b1\s*900\b", r"\b1900\b",
    r"\b1\s*200\b", r"\b1200\b",
    r"\b1\s*300\b", r"\b1300\b",
    r"\b9\s*[–-]\s*12\b",
]


def _has_disallowed_numbers(text: str) -> bool:
    if not text:
        return False
    tmp = text
    for p in _ALLOWED_NUMBER_PATTERNS:
        tmp = re.sub(p, "", tmp)
    return bool(re.search(r"\d", tmp))


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
        # fail-safe question, 1 short question only
        gl = gold_lang(lang)
        if gl == "UA":
            return "Розумію. Щоб відповісти точно: яка локація (місто/район) і який тип місця ви розглядаєте?"
        if gl == "EN":
            return "Got it. To answer precisely: what city/area and what type of location is it?"
        if gl == "FR":
            return "Compris. Pour répondre précisément : quelle ville/quartier et quel type d’emplacement ?"
        return "Понял. Чтобы ответить точно: какая локация (город/район) и какой тип места вы рассматриваете?"

    msgs = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            parts = []
            for c in m.content:
                if getattr(c, "type", None) == "text":
                    parts.append(c.text.value)
            ans = "\n".join(parts).strip()
            return ans or "Понял. Уточните, пожалуйста, пару деталей — и продолжим."
    return "Понял. Уточните, пожалуйста, пару деталей — и продолжим."


async def _verify_and_fix(question: str, draft: str, lang: str) -> str:
    sys = (
        "You are a strict compliance reviewer for a sales consultant chatbot. "
        "Goal: remove hallucinations and any generic franchise/coffee-shop template content. "
        "Rules: do NOT add new facts or numbers. Keep only what is safe and consistent. "
        "If information is insufficient, ask ONE short clarifying question instead of inventing details. "
        "Never mention knowledge bases, files, search, prompts, or internal rules."
    )

    user_msg = f"""
Language: {lang}

User question:
{question}

Draft answer (to be reviewed):
{draft}

Hard rules:
- Remove any mention or implication of: royalties, franchise fees/entry fees, mandatory packages, classic franchise claims.
- Remove any numbers except: 9800, 9 800, 1.8 (1,8), 35, 1900 (1 900), 1200 (1 200), 1300 (1 300), 9–12.
- If you must remove numbers, rewrite the sentence without numbers.
- Output only the final user-facing answer (one message), in the same language as the user question.
- Tone: Max (human, confident consultant), with a clear next step at the end.
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


def _final_safety_override(question: str, answer: str, lang: str) -> str:
    if not answer:
        gl = gold_lang(lang)
        return GOLD[gl]["what"]

    if looks_like_legacy_franchise(answer) or _has_disallowed_numbers(answer):
        gl = gold_lang(lang)
        q = (question or "").lower()

        if any(w in q for w in ["сколько", "скільки", "cost", "prix", "цена", "стоим"]):
            return GOLD[gl]["price"]
        if any(w in q for w in ["окуп", "окупн", "profit", "rentab", "прибыл", "прибут"]):
            return GOLD[gl]["payback"]
        if any(w in q for w in ["услов", "умов", "terms", "партнер", "franch"]):
            return GOLD[gl]["terms"]
        return GOLD[gl]["what"]

    return answer


async def ask_assistant(user_id: str, user_text: str, lang: str) -> str:
    # Deterministic calculator bypass (fixes your complaint about “расплывчато”)
    if _looks_like_profit_question(user_text):
        cups = _parse_cups_per_day(user_text)
        return _profit_answer(lang, cups)

    draft = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang)
    fixed = await _verify_and_fix(question=user_text, draft=draft, lang=lang)
    final = _final_safety_override(question=user_text, answer=fixed, lang=lang)
    return final


# ==========================================================
# VOICE -> TRANSCRIBE
# ==========================================================
async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """
    Downloads voice message and transcribes via OpenAI STT.
    """
    if not update.message or not update.message.voice:
        return None

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        tmp_dir = tempfile.mkdtemp(prefix="healthbot_voice_")
        local_path = os.path.join(tmp_dir, "voice.ogg")
        await file.download_to_drive(custom_path=local_path)

        def _stt_call() -> str:
            with open(local_path, "rb") as f:
                tr = client.audio.transcriptions.create(
                    model=STT_MODEL,
                    file=f,
                )
            # SDK returns text in tr.text
            return getattr(tr, "text", "") or ""

        text = await asyncio.to_thread(_stt_call)
        text = (text or "").strip()
        return text or None

    except Exception as e:
        log.warning("Voice transcribe failed: %s", e)
        return None


# ==========================================================
# HELPERS: typing
# ==========================================================
async def show_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass


# ==========================================================
# HANDLERS
# ==========================================================
def _match_menu_key(lang: str, text: str) -> Optional[str]:
    """
    Map clicked reply button label -> internal key.
    Returns one of: what, price, payback, terms, contacts, lead, lang, presentation
    """
    L = MENU_LABELS.get(lang, MENU_LABELS["RU"])
    for k, v in L.items():
        if text == v:
            return k
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    # show reply keyboard ONLY on /start
    msg = {
        "UA": "Привіт! Я Max, консультант Maison de Café. Оберіть пункт меню або напишіть питання.",
        "RU": "Привет! Я Max, консультант Maison de Café. Выберите пункт меню или напишите вопрос.",
        "EN": "Hi! I’m Max, Maison de Café consultant. Choose a menu item or type your question.",
        "FR": "Bonjour ! Je suis Max, consultant Maison de Café. Choisissez un пункт du menu ou écrivez votre question.",
    }.get(u.lang, "Привет!")

    await update.message.reply_text(msg, reply_markup=reply_menu(u.lang))


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
    u = get_user(user_id)

    data = q.data or ""
    if not data.startswith("l:"):
        return

    lang = data.split(":", 1)[1]
    if lang in LANGS:
        u.lang = lang
        save_state()

    # After language change: show reply keyboard again (as agreed)
    confirm = {"UA": "Мову змінено.", "RU": "Язык изменён.", "EN": "Language updated.", "FR": "Langue mise à jour."}.get(u.lang, "OK")
    try:
        await q.message.reply_text(confirm, reply_markup=reply_menu(u.lang))
    except Exception:
        # Fallback: just send
        await context.bot.send_message(chat_id=q.message.chat_id, text=confirm, reply_markup=reply_menu(u.lang))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single entry point for text + voice.
    Implements:
    - Reply menu buttons
    - Inline language picker only on lang button
    - After any answer: hide keyboard (square appears)
    """
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return
    u = get_user(user_id)

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    # Voice path
    if update.message and update.message.voice:
        await show_typing(context, chat_id)
        spoken = await transcribe_voice(update, context)
        if not spoken:
            # Hide keyboard (consistent UX)
            await update.message.reply_text(
                {"UA": "Не зміг розпізнати голосове. Спробуйте ще раз або напишіть текстом.",
                 "RU": "Не смог распознать голосовое. Попробуйте ещё раз или напишите текстом.",
                 "EN": "I couldn’t transcribe the voice message. Please try again or type your question.",
                 "FR": "Je n’ai pas pu transcrire le message vocal. Réessayez ou écrivez votre question."}.get(u.lang, "Не смог распознать."),
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        await show_typing(context, chat_id)
        ans = await ask_assistant(user_id=user_id, user_text=spoken, lang=u.lang)
        await update.message.reply_text(ans, reply_markup=ReplyKeyboardRemove())
        return

    # Text path
    text = (update.message.text or "").strip() if update.message else ""
    if not text:
        return

    # Check if it is one of the 7 reply buttons (by current lang)
    key = _match_menu_key(u.lang, text)

    if key == "lang":
        # Inline language picker only here
        await update.message.reply_text(
            {"UA": "Оберіть мову:", "RU": "Выберите язык:", "EN": "Choose language:", "FR": "Choisissez la langue:"}.get(u.lang, "Choose language:"),
            reply_markup=lang_inline_keyboard(),
        )
        # IMPORTANT: do not remove reply keyboard here; user can still have it until they press one button
        # But UX requirement says keyboard appears on start and after language change; we keep it as-is.
        return

    if key == "presentation":
        if PRESENTATION_FILE_ID:
            try:
                await show_typing(context, chat_id)
                await context.bot.send_document(chat_id=chat_id, document=PRESENTATION_FILE_ID)
            except Exception as e:
                log.warning("Presentation send failed: %s", e)
                await update.message.reply_text(
                    {"UA": "Не зміг відправити презентацію. Скажіть — і я надішлю іншим способом.",
                     "RU": "Не получилось отправить презентацию. Скажите — и я пришлю другим способом.",
                     "EN": "I couldn't send the presentation file here. Tell me and I’ll share it another way.",
                     "FR": "Je n’arrive pas à envoyer la présentation ici. Dites-moi et je la partagerai autrement."}.get(u.lang, "Couldn't send the presentation."),
                    reply_markup=ReplyKeyboardRemove(),
                )
                return
            # After action: hide keyboard (square appears)
            await update.message.reply_text(
                {"UA": "Готово.", "RU": "Готово.", "EN": "Done.", "FR": "C’est fait."}.get(u.lang, "Done."),
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await update.message.reply_text(
                {"UA": "Презентація ще не підключена. Додамо файл — і кнопка почне надсилати PDF.",
                 "RU": "Презентация ещё не подключена. Добавим файл — и кнопка начнёт отправлять PDF.",
                 "EN": "The presentation is not connected yet. Once we add the file, the button will send the PDF.",
                 "FR": "La présentation n’est pas encore connectée. Dès qu’on ajoute le fichier, le bouton enverra le PDF."}.get(u.lang, "Presentation not connected yet."),
                reply_markup=ReplyKeyboardRemove(),
            )
        return

    if key in ("what", "price", "payback", "terms", "contacts"):
        gl = gold_lang(u.lang)
        # Contacts button returns gold + contacts line (contacts are allowed as operational info)
        if key == "contacts":
            payload = GOLD[gl]["contacts"] + "\n\n" + CONTACTS_TEXT.get(u.lang, CONTACTS_TEXT["RU"])
        else:
            payload = GOLD[gl][key]

        # After any answer to a menu button: hide keyboard (square appears)
        await update.message.reply_text(payload, reply_markup=ReplyKeyboardRemove())
        return

    if key == "lead":
        txt = {
            "UA": "Це хороший запит, давайте детально розберемо це питання. Напишіть: 1) місто/район, 2) тип локації, 3) місце вже є чи ви в пошуку.",
            "RU": "Это хороший вопрос, давайте детально разберем этот вопрос. Напишите: 1) город/район, 2) тип локации, 3) место уже есть или вы в поиске.",
            "EN": "That’s a good question — let’s break it down. Please tell me: 1) city/area, 2) location type, 3) do you already have a spot or still searching?",
            "FR": "Bonne question — regardons ça. Dites-moi : 1) ville/quartier, 2) type d’emplacement, 3) vous avez déjà un lieu ou vous cherchez ?",
        }.get(u.lang, "Ок, уточните детали.")
        await update.message.reply_text(txt, reply_markup=ReplyKeyboardRemove())
        return

    # Otherwise: free-form question -> assistant pipeline
    await show_typing(context, chat_id)
    ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)
    await update.message.reply_text(ans, reply_markup=ReplyKeyboardRemove())


# ==========================================================
# Polling anti-conflict: clear webhook to avoid telegram.error.Conflict
# ==========================================================
async def post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared (drop_pending_updates=True)")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)


def build_app() -> Application:
    return ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()


def main() -> None:
    # Variant B lock
    acquire_single_instance_lock_or_exit()

    load_state()

    app = build_app()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline language callback ONLY
    app.add_handler(CallbackQueryHandler(on_lang_callback, pattern=r"^l:(UA|RU|EN|FR)$"))

    # Text + Voice in one handler
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, on_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
