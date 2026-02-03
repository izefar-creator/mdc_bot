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

"""
This version of the bot implements several improvements based on user feedback:

1. **Persistent reply keyboard** – after every response the menu buttons stay
   visible instead of being removed.  This is achieved by sending the
   appropriate reply keyboard with every message rather than using
   `ReplyKeyboardRemove()`.
2. **Multilingual golden answers** – the five base questions now have
   pre‑defined answers not only in Russian but also in Ukrainian, English
   and French.  These deterministic answers are used whenever the user
   selects one of the menu items so that the bot doesn’t call the
   language model unnecessarily.
3. **Simple spam filter** – messages consisting solely of punctuation,
   URLs or excessive repeated characters are treated as spam.  The bot
   politely asks the user to choose a menu item or rephrase instead of
   forwarding such messages to the assistant.
4. **Refined voice handling** – after processing a voice message, the
   bot always sends the reply keyboard again so that the user can
   continue the conversation smoothly.

The rest of the logic (state management, assistant integration, etc.)
remains largely unchanged from the original implementation.
"""

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

# GOLD answers (5 benchmarks) in four languages
GOLD_5: Dict[str, Dict[str, str]] = {
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
            "В эту сумму входит профессиональный автомат Jetinno JL-300, фирменная стойка, телеметрия, стартовый набор ингредиентов, "
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
    },
    "UA": {
        "what": (
            "Гарне запитання, з нього зазвичай починається знайомство. "
            "Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії. "
            "Ви отримуєте професійну кавову машину Jetinno JL-300, фірмову стійку, систему контролю та стартовий набір інгредієнтів, "
            "а також навчання та супровід запуску. Формат розрахований на швидкий старт без досвіду в кавовому бізнесі і роботу без персоналу. "
            "Далі логічно або розібрати вартість запуску, або подивитися на окупність і реальні цифри."
        ),
        "price": (
            "Гарне запитання, давайте детально розберемо. "
            "Базова вартість запуску точки Maison de Café в Бельгії становить 9 800 €. "
            "До цієї суми входить професійний автомат Jetinno JL-300, фірмова стійка, телеметрія, стартовий набір інгредієнтів, "
            "навчання та повний запуск. Це не франшиза з пакетами та прихованими платежами — ви платите за конкретне обладнання та сервіс. "
            "Окремо зазвичай враховуються лише речі, що залежать від вашої ситуації, наприклад оренда локації або електрика. "
            "Далі логічно або подивитися окупність, або обговорити вашу майбутню локацію."
        ),
        "payback": (
            "Гарне запитання, без розуміння цифр справді нема сенсу йти далі. "
            "У базовій моделі Maison de Café середня маржа з однієї чашки становить близько 1,8 €, а типовий обсяг продажів — приблизно 35 чашок на день. "
            "Це дає валову маржу близько 1 900 € на місяць, з якої після стандартних витрат зазвичай залишається близько 1 200–1 300 € чистого прибутку. "
            "За таких показників точка виходить на окупність у середньому за 9–12 місяців, але реальний результат завжди залежить від локації та потоку людей. "
            "Можемо розібрати конкретне місце або перейти до умов співпраці."
        ),
        "terms": (
            "Гарне запитання, це важливий момент — і тут часто бувають неправильні очікування. "
            "Maison de Café — це не класична франшиза з жорсткими правилами та паушальними внесками. "
            "Це партнерська модель: ви інвестуєте в обладнання та управляєте точкою, а ми забезпечуємо продукт, стандарти якості, "
            "навчання та підтримку на старті. У вас залишається свобода у виборі локації та управлінні бізнесом. "
            "Можемо обговорити вашу ідею або перейти до наступного кроку."
        ),
        "contacts": (
            "Гарне запитання. Якщо ви дійшли до цього етапу, значить формат вам справді цікавий. "
            "Найкорисніший наступний крок — коротко обговорити вашу ситуацію: локацію, бюджет і очікування. "
            "Так стає зрозуміло, наскільки Maison de Café підходить саме вам, без теорії та зайвих обіцянок. "
            "Ми можемо або оформити заявку і розібрати все персонально, або повернутися до цифр і ще раз спокійно пройтися по окупності.\n\n"
            f"{CONTACTS_TEXT['UA']}"
        ),
    },
    "EN": {
        "what": (
            "Good question—this is usually the starting point. "
            "Maison de Café is a turnkey self‑service coffee point in Belgium. "
            "You get a professional Jetinno JL-300 machine, a branded counter, a control system and a starter set of ingredients, "
            "along with training and launch support. The format is designed for a quick start without experience in the coffee business and for operation without staff. "
            "The next logical step is to discuss the opening cost or look at payback and real numbers."
        ),
        "price": (
            "Good question—let’s go into detail. "
            "The base cost to launch a Maison de Café point in Belgium is €9 800. "
            "This includes the professional Jetinno JL‑300 machine, branded counter, telemetry, starter ingredients, training and full launch. "
            "It’s not a franchise with packages and hidden fees—you pay for specific equipment and service. "
            "Only items dependent on your situation, like location rent or electricity, are usually extra. "
            "Next logical steps are to look at payback or discuss your future location."
        ),
        "payback": (
            "Good question—without understanding the numbers there is no point going further. "
            "In the basic model, the average margin per cup is about €1.8, and the typical sales volume is around 35 cups per day. "
            "This yields a gross margin of roughly €1 900 per month, from which after standard expenses there is usually about €1 200–1 300 net profit. "
            "With such figures, a point reaches payback in about 9–12 months, but the real result always depends on location and foot traffic. "
            "We can analyse a specific site or move to partnership terms."
        ),
        "terms": (
            "Good question—this is an important point, and expectations are often wrong here. "
            "Maison de Café is not a classic franchise with strict rules and lump‑sum fees. "
            "It’s a partnership model: you invest in the equipment and operate the point, and we provide the product, quality standards, training and support at the start. "
            "You retain freedom in choosing the location and managing the business. "
            "We can discuss your idea or move to the next step."
        ),
        "contacts": (
            "Good question. If you’ve reached this stage, the format really interests you. "
            "The most helpful next step is to briefly discuss your situation: location, budget and expectations. "
            "It becomes clear how well Maison de Café suits you, without theory and unnecessary promises. "
            "We can either submit a request and go over everything individually, or return to the numbers and calmly review payback again.\n\n"
            f"{CONTACTS_TEXT['EN']}"
        ),
    },
    "FR": {
        "what": (
            "Bonne question — c’est généralement par là qu’on commence. "
            "Maison de Café est un point de vente en libre service clé en main en Belgique. "
            "Vous recevez une machine à café professionnelle Jetinno JL‑300, un comptoir personnalisé, un système de contrôle et un kit de démarrage d’ingrédients, "
            "ainsi que la formation et l’accompagnement pour le lancement. Le format est conçu pour un démarrage rapide sans expérience dans le domaine du café et pour fonctionner sans personnel. "
            "Ensuite, il est logique de discuter du coût de lancement ou d’examiner la rentabilité et les chiffres réels."
        ),
        "price": (
            "Bonne question — analysons en détail. "
            "Le coût de lancement d’un point Maison de Café en Belgique est de 9 800 €. "
            "Cette somme comprend la machine professionnelle Jetinno JL‑300, le comptoir de marque, la télémétrie, le kit de démarrage d’ingrédients, la formation et le lancement complet. "
            "Ce n’est pas une franchise avec des packs et des frais cachés — vous payez pour un équipement et un service spécifiques. "
            "Seuls les éléments qui dépendent de votre situation, comme le loyer de l’emplacement ou l’électricité, sont généralement en supplément. "
            "Ensuite, il est logique de regarder la rentabilité ou de discuter de votre futur emplacement."
        ),
        "payback": (
            "Bonne question — sans comprendre les chiffres, cela ne sert à rien d’aller plus loin. "
            "Dans le modèle de base Maison de Café, la marge moyenne par tasse est d’environ 1,8 €, et le volume de vente typique est d’environ 35 tasses par jour. "
            "Cela donne une marge brute d’environ 1 900 € par mois, dont, après les dépenses standard, il reste généralement environ 1 200–1 300 € de bénéfice net. "
            "Avec de tels chiffres, un point atteint la rentabilité en moyenne en 9–12 mois, mais le résultat réel dépend toujours de l’emplacement et du flux de clients. "
            "Nous pouvons analyser un site spécifique ou passer aux conditions de partenariat."
        ),
        "terms": (
            "Bonne question — c’est un point important, où les attentes sont souvent erronées. "
            "Maison de Café n’est pas une franchise classique avec des règles strictes et des droits d’entrée. "
            "C’est un modèle de partenariat : vous investissez dans l’équipement et gérez le point, et nous fournissons le produit, les standards de qualité, la formation et l’accompagnement au démarrage. "
            "Vous gardez la liberté dans le choix de l’emplacement et la gestion de l’activité. "
            "Nous pouvons discuter de votre idée ou passer à l’étape suivante."
        ),
        "contacts": (
            "Bonne question. Si vous êtes arrivé à ce stade, c’est que le format vous intéresse vraiment. "
            "L’étape suivante la plus utile est de discuter brièvement de votre situation : emplacement, budget et attentes. "
            "Cela permet de comprendre à quel point Maison de Café vous convient, sans théorie ni promesses inutiles. "
            "Nous pouvons soit remplir une demande et tout examiner individuellement, soit revenir aux chiffres et revoir calmement la rentabilité.\n\n"
            f"{CONTACTS_TEXT['FR']}"
        ),
    },
}


def reply_menu(lang: str) -> ReplyKeyboardMarkup:
    """Return the persistent reply keyboard for a given language."""
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
        one_time_keyboard=True,
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


def is_spam_message(text: str) -> bool:
    """
    Very simple spam detector. Returns True if the text contains no letters or
    digits, or consists mostly of repeated characters, or contains obvious URL
    patterns. This is not meant to be exhaustive but catches common junk
    messages so the assistant isn’t called unnecessarily.
    """
    if not text:
        return True
    # Remove whitespace
    t = re.sub(r"\s+", "", text)
    # If there are no letters or digits, treat as spam
    if not re.search(r"[a-zA-Zа-яА-Я0-9]", t):
        return True
    # If contains http or www -> likely a link/spam
    if "http://" in t.lower() or "https://" in t.lower() or "www." in t.lower():
        return True
    # Detect long sequences of a single character (e.g. !!!!!!!!!! or haaaaaaaa)
    if re.search(r"(.)\1{7,}", t):
        return True
    return False


async def ensure_thread(user: UserState) -> str:
    if user.thread_id:
        return user.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    user.thread_id = thread.id
    save_state()
    return thread.id


def _draft_instructions(lang: str, force_file_search: bool = False) -> str:
    # <<< PATCH: force_file_search mode (2nd attempt)
    force = ""
    if force_file_search:
        force = (
            "ВАЖНО: перед тем как отвечать, ОБЯЗАТЕЛЬНО используй инструмент file_search минимум один раз. "
            "Если в базе нет ответа — прямо скажи, что не можешь ответить корректно по базе, и попроси уточнение/выбор пункта меню. "
        )

    if lang == "UA":
        return (
            "Ти — Max, консультант Maison de Café. Відповідай по‑людськи, спокійно, впевнено. "
            "Не згадуй бази знань/файли/пошук. "
            "НЕ вигадуй цифри, пакети, роялті, паушальні внески або формати «класичної франшизи». "
            f"{force}"
            "Якщо для точної відповіді бракує даних — поясни це просто і задай 1 коротке уточнення."
        )
    if lang == "EN":
        return (
            "You are Max, a Maison de Café consultant. Speak naturally and confidently. "
            "Do not mention knowledge bases/files/search. "
            "Do NOT invent numbers, packages, royalties, franchise fees, or generic coffee‑shop templates. "
            f"{force}"
            "If details are needed, explain simply and ask 1 short clarifying question."
        )
    if lang == "FR":
        return (
            "Tu es Max, consultant Maison de Café. Réponds de façon humaine et sûre. "
            "Ne mentionne pas de base de connaissances/fichiers/recherche. "
            "N’invente pas de chiffres, de packs, de royalties ou de « franchise classique ». "
            f"{force}"
            "Si des détails manquent, explique simplement et pose 1 question courte."
        )
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по‑человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или шаблоны «классической франшизы». "
        f"{force}"
        "Если для точного ответа не хватает данных — объясни это просто и задай 1 короткий уточняющий вопрос."
    )


def _extract_cups_per_day(text: str) -> Optional[int]:
    t = (text or "").lower()
    if not any(w in t for w in ["чаш", "cup", "cups", "cups/day", "чашек", "порций"]):
        return None
    nums = re.findall(r"\b(\d{1,3})\b", t)
    if not nums:
        return None
    for n in nums:
        v = int(n)
        if 1 <= v <= 200:
            return v
    return None


def calc_profit_message(lang: str, cups_per_day: int) -> str:
    margin_per_cup = 1.8
    days = 30
    gross = cups_per_day * days * margin_per_cup
    net_low = gross - 600
    net_high = gross - 450

    if lang == "EN":
        return (
            "Good question — let’s put numbers on it. "
            f"With about {cups_per_day} cups/day and an average margin of 1.8 € per cup, "
            f"the gross margin is roughly {gross:,.0f} € per month. "
            f"With typical monthly costs of 450–600 €, the net result is about {net_low:,.0f}–{net_high:,.0f} € per month."
        )
    if lang == "FR":
        return (
            "Bonne question — mettons des chiffres dessus. "
            f"Avec environ {cups_per_day} tasses/jour et une marge moyenne de 1,8 € par tasse, "
            f"la marge brute est d’environ {gross:,.0f} € par mois. "
            f"Avec des coûts mensuels typiques de 450–600 €, le résultat net est d’environ {net_low:,.0f}–{net_high:,.0f} € par mois."
        )
    if lang == "UA":
        return (
            "Хороший запит — давайте по цифрах. "
            f"За обсягу приблизно {cups_per_day} чашок/день і середньої маржі 1,8 € з чашки, "
            f"валова маржа виходить близько {gross:,.0f} € на місяць. "
            f"За типових витрат 450–600 € на місяць чистий результат — орієнтовно {net_low:,.0f}–{net_high:,.0f} € на місяць."
        )
    return (
        "Хороший вопрос — давайте по цифрам. "
        f"При объёме примерно {cups_per_day} чашек в день и средней марже 1,8 € с чашки "
        f"валовая маржа выходит около {gross:,.0f} € в месяц. "
        f"При типичных ежемесячных расходах 450–600 € чистый результат — ориентировочно {net_low:,.0f}–{net_high:,.0f} € в месяц."
    )


def _kb_only_fallback(lang: str) -> str:
    if lang == "EN":
        return "I can’t answer correctly from the knowledge base. Please choose a menu item or уточните вопрос."
    if lang == "FR":
        return "Je ne peux pas répondre correctement selon la base. Choisissez un пункт du menu ou уточните вопрос."
    if lang == "UA":
        return "Я не можу відповісти коректно по базі. Оберіть пункт меню або уточніть питання."
    return "Я не могу ответить корректно по базе. Выберите пункт меню или уточните вопрос."


async def _run_used_file_search(thread_id: str, run_id: str) -> bool:
    """
    Returns True if any run step contains a tool call of type 'file_search'.
    """
    try:
        steps = await asyncio.to_thread(
            client.beta.threads.runs.steps.list,
            thread_id=thread_id,
            run_id=run_id,
            limit=50,
        )
        for st in getattr(steps, "data", []) or []:
            details = getattr(st, "step_details", None)
            if not details:
                continue
            # SDK objects may vary; we check robustly
            # Common shape: details.type == "tool_calls" and details.tool_calls[*].type == "file_search"
            d_type = getattr(details, "type", None) or getattr(details, "kind", None)
            if d_type == "tool_calls":
                tool_calls = getattr(details, "tool_calls", None) or []
                for tc in tool_calls:
                    tc_type = getattr(tc, "type", None) or getattr(tc, "tool", None)
                    if tc_type == "file_search":
                        return True
                    # Sometimes nested: tc.file_search exists
                    if getattr(tc, "file_search", None) is not None:
                        return True
        return False
    except Exception as e:
        log.warning("steps.list failed: %s", e)
        return False


async def _assistant_draft(user_id: str, user_text: str, lang: str, force_file_search: bool) -> Tuple[str, bool]:
    """
    Returns (answer_text, file_search_used)
    """
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
        instructions=_draft_instructions(lang, force_file_search=force_file_search),
    )

    deadline = time.time() + 45
    while time.time() < deadline:
        rs = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)
        if rs.status in ("completed", "failed", "cancelled", "expired"):
            run = rs
            break
        await asyncio.sleep(0.7)

    if getattr(run, "status", "") != "completed":
        return ("", False)

    fs_used = await _run_used_file_search(thread_id=thread_id, run_id=run.id)

    msgs = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
    for m in msgs.data:
        if m.role == "assistant":
            parts = []
            for c in m.content:
                if getattr(c, "type", None) == "text":
                    parts.append(c.text.value)
            ans = "\n".join(parts).strip()
            return (ans or "", fs_used)

    return ("", fs_used)


async def ask_assistant(user_id: str, user_text: str, lang: str) -> str:
    # Deterministic calculator override
    cups = _extract_cups_per_day(user_text)
    if cups is not None:
        return calc_profit_message(lang=lang, cups_per_day=cups)

    # Run #1 (normal)
    ans1, fs1 = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang, force_file_search=False)
    if fs1 and ans1:
        return ans1

    # Run #2 (FORCE file_search)
    ans2, fs2 = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang, force_file_search=True)
    if fs2 and ans2:
        return ans2

    # Hard fallback (KB-only rule)
    return _kb_only_fallback(lang)


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

    # show reply keyboard again after language change
    await q.message.reply_text(confirm, reply_markup=reply_menu(u.lang))


async def send_presentation(chat_id: int, lang: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the presentation document or notify the user if missing, keeping the menu visible."""
    if not PRESENTATION_FILE_ID:
        msg = {
            "UA": "Гарне запитання. Презентація ще не підключена — додамо файл і я одразу зможу її надіслати.",
            "RU": "Хороший вопрос. Презентация ещё не подключена — добавим файл и я сразу смогу её отправить.",
            "EN": "Good question. The presentation isn’t connected yet — once the file is added, I can send it right away.",
            "FR": "Bonne question. La présentation n’est pas encore connectée — dès que le fichier est ajouté, je peux l’envoyer.",
        }.get(lang, "Презентация ещё не подключена.")
        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=reply_menu(lang))
        return

    try:
        await context.bot.send_document(chat_id=chat_id, document=PRESENTATION_FILE_ID)
        # After sending the document, send a blank message to re-display the menu
        await context.bot.send_message(chat_id=chat_id, text=" ", reply_markup=reply_menu(lang))
    except Exception as e:
        log.warning("Presentation send failed: %s", e)
        msg = {
            "UA": "Гарне запитання. Не зміг відправити презентацію в цьому чаті. Напишіть — і я надішлю іншим способом.",
            "RU": "Хороший вопрос. Не получилось отправить презентацию в этом чате. Напишите — и я пришлю другим способом.",
            "EN": "Good question. I couldn’t send the presentation here. Message me and I’ll share it another way.",
            "FR": "Bonne question. Je n’arrive pas à envoyer la présentation ici. Écrivez-moi et je la partagerai autrement.",
        }.get(lang, "Не получилось отправить презентацию.")
        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=reply_menu(lang))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    text = (update.message.text or "").strip()
    if not text:
        return

    async with get_user_lock(user_id):
        # Spam filter: handle obviously junk messages politely
        if is_spam_message(text):
            polite = {
                "UA": "Вибачте, не зрозумів запит. Оберіть пункт меню або поставте уточнювальне питання.",
                "RU": "Извините, не понял запрос. Выберите пункт меню или уточните вопрос.",
                "EN": "Sorry, I didn’t understand. Please choose a menu item or clarify.",
                "FR": "Désolé, je n’ai pas compris. Choisissez un élément du menu ou clarifiez."
            }.get(u.lang, "Извините, не понял запрос.")
            await update.message.reply_text(polite, reply_markup=reply_menu(u.lang))
            return

        action = match_menu_action(u.lang, text)

        if action == "lang":
            prompt = {"UA": "Оберіть мову:", "RU": "Выберите язык:", "EN": "Choose language:", "FR": "Choisissez la langue:"}.get(u.lang, "Выберите язык:")
            await update.message.reply_text(prompt, reply_markup=lang_inline_keyboard())
            return

        if action == "presentation":
            await send_presentation(chat_id=update.effective_chat.id, lang=u.lang, context=context)
            return

        # Pre‑defined answers for menu actions
        if action in ("what", "price", "payback", "terms", "contacts"):
            if action in GOLD_5.get(u.lang, {}):
                # Use deterministic answer and redisplay menu
                ans = GOLD_5[u.lang][action]
                await update.message.reply_text(ans, reply_markup=reply_menu(u.lang))
            else:
                # Fallback to assistant for languages without gold answers
                stop = asyncio.Event()
                typing_task = asyncio.create_task(_typing_loop(context, update.effective_chat.id, stop))
                try:
                    ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)
                finally:
                    stop.set()
                    await typing_task
                await update.message.reply_text(ans, reply_markup=reply_menu(u.lang))
            return

        # Free text -> KB-only gate pipeline
        stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(context, update.effective_chat.id, stop))
        try:
            ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)
        finally:
            stop.set()
            await typing_task

        await update.message.reply_text(ans, reply_markup=reply_menu(u.lang))


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

            with open(ogg_path, "rb") as f:
                tr = await asyncio.to_thread(
                    client.audio.transcriptions.create,
                    model=TRANSCRIBE_MODEL,
                    file=f,
                )
            transcript = (getattr(tr, "text", "") or "").strip()

            if not transcript:
                msg = {
                    "UA": "Гарне запитання. Не зміг розпізнати голос. Спробуйте ще раз коротше й чіткіше.",
                    "RU": "Хороший вопрос. Не смог распознать голос. Попробуйте ещё раз короче и чётче.",
                    "EN": "Good question. I couldn’t transcribe the voice message. Please try again, shorter and clearer.",
                    "FR": "Bonne question. Je n’ai pas pu transcrire le message vocal. Réessayez plus court et plus clair.",
                }.get(u.lang, "Не смог распознать голос.")
                await update.message.reply_text(msg, reply_markup=reply_menu(u.lang))
                return

            ans = await ask_assistant(user_id=user_id, user_text=transcript, lang=u.lang)
            await update.message.reply_text(ans, reply_markup=reply_menu(u.lang))
        finally:
            stop.set()
            await typing_task


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

    app.add_handler(CallbackQueryHandler(on_lang_callback, pattern=r"^LANG:"))

    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
