import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

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


# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "").strip()

OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID", "").strip()

# Telegram file_id for the presentation PDF (one file for all languages)
PRESENTATION_FILE_ID = os.getenv("PRESENTATION_FILE_ID", "").strip()

# 2-pass verifier model (no KB access)
VERIFY_MODEL = os.getenv("VERIFY_MODEL", "gpt-4o-mini").strip()

# Voice transcription model
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")
if not ASSISTANT_ID:
    raise RuntimeError("ASSISTANT_ID missing")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# LOGGING
# =========================
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


# =========================
# SINGLETON LOCK (Variant B)
# - prevents accidental double polling process (telegram.error.Conflict)
# =========================
LOCK_PATH = os.getenv("BOT_LOCK_PATH", "/tmp/maisondecafe_bot.lock").strip()


def acquire_singleton_lock_or_exit() -> None:
    """
    Linux/Render-safe singleton lock via fcntl.
    If another process holds lock -> exit immediately.
    """
    try:
        import fcntl  # Linux-only, OK for Render
        fp = open(LOCK_PATH, "w")
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("Another bot process is already running (lock=%s). Exiting.", LOCK_PATH)
            raise SystemExit(0)

        # Keep reference alive for the lifetime of process
        globals()["_LOCK_FD"] = fp
        log.info("Singleton lock acquired: %s", LOCK_PATH)
    except Exception as e:
        # If lock mechanism fails, do NOT crash the bot; log and continue.
        log.warning("Singleton lock not enforced (%s). Continuing.", e)


# =========================
# STATE (persisted)
# =========================
STATE_FILE = Path("healthbot_state.json")


@dataclass
class UserState:
    lang: str = "UA"       # UA/RU/EN/FR
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


# =========================
# UX: Reply keyboard (fixed 6–7 buttons) + Inline only for language
# Rule:
# - Reply keyboard is sent ONLY on /start and after language change
# - All normal replies are sent with ReplyKeyboardRemove() so keyboard hides
# - User can open it again via the Telegram “square” icon
# =========================

MENU = {
    "UA": {
        "b_what": "☕ Що таке Maison de Café?",
        "b_price": "💶 Скільки коштує відкрити?",
        "b_payback": "📈 Окупність і прибуток",
        "b_terms": "🤝 Умови співпраці",
        "b_contacts": "📞 Контакти",
        "b_presentation": "📄 Презентація",
        "b_lang": "🌍 Мова",
    },
    "RU": {
        "b_what": "☕ Что такое Maison de Café?",
        "b_price": "💶 Сколько стоит открыть?",
        "b_payback": "📈 Окупаемость и прибыль",
        "b_terms": "🤝 Условия сотрудничества",
        "b_contacts": "📞 Контакты",
        "b_presentation": "📄 Презентация",
        "b_lang": "🌍 Язык",
    },
    "EN": {
        "b_what": "☕ What is Maison de Café?",
        "b_price": "💶 Opening cost",
        "b_payback": "📈 Payback & profit",
        "b_terms": "🤝 Partnership terms",
        "b_contacts": "📞 Contacts",
        "b_presentation": "📄 Presentation",
        "b_lang": "🌍 Language",
    },
    "FR": {
        "b_what": "☕ Qu’est-ce que Maison de Café ?",
        "b_price": "💶 Coût de lancement",
        "b_payback": "📈 Rentabilité & profit",
        "b_terms": "🤝 Conditions",
        "b_contacts": "📞 Contacts",
        "b_presentation": "📄 Présentation",
        "b_lang": "🌍 Langue",
    },
}


def reply_menu(lang: str) -> ReplyKeyboardMarkup:
    L = MENU.get(lang, MENU["UA"])
    # 2 columns to reduce vertical height, keeps “Presentation” visible
    keyboard = [
        [KeyboardButton(L["b_what"]), KeyboardButton(L["b_price"])],
        [KeyboardButton(L["b_payback"]), KeyboardButton(L["b_terms"])],
        [KeyboardButton(L["b_contacts"]), KeyboardButton(L["b_presentation"])],
        [KeyboardButton(L["b_lang"])],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
        input_field_placeholder=None,
    )


def lang_inline_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(LANG_LABELS["UA"], callback_data="l:UA"),
         InlineKeyboardButton(LANG_LABELS["RU"], callback_data="l:RU")],
        [InlineKeyboardButton(LANG_LABELS["EN"], callback_data="l:EN"),
         InlineKeyboardButton(LANG_LABELS["FR"], callback_data="l:FR")],
    ]
    return InlineKeyboardMarkup(kb)


CONTACTS_TEXT = {
    "UA": "Контакти Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "RU": "Контакты Maison de Café:\n• Email: maisondecafe.coffee@gmail.com\n• Телефон: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "EN": "Maison de Café contacts:\n• Email: maisondecafe.coffee@gmail.com\n• Phone: +32 470 600 806\n• Telegram: https://t.me/maisondecafe",
    "FR": "Contacts Maison de Café:\n• Email : maisondecafe.coffee@gmail.com\n• Téléphone : +32 470 600 806\n• Telegram : https://t.me/maisondecafe",
}


# =========================
# GOLD answers (button-safe)
# =========================
GOLD = {
    "UA": {
        "what": (
            "Хороший запит — з цього зазвичай і починається знайомство. "
            "Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії: професійний автомат Jetinno JL-300, "
            "фірмова стійка, система контролю та стартовий набір інгредієнтів, плюс навчання і супровід запуску. "
            "Формат розрахований на швидкий старт без досвіду та роботу без персоналу. "
            "Що вам зручніше далі: розібрати вартість запуску чи одразу пройтися по окупності й цифрах?"
        ),
        "price": (
            "Це найлогічніше питання — і тут важливо говорити чесно. "
            "Базова вартість запуску точки Maison de Café в Бельгії — 9 800 €. "
            "У цю суму входить Jetinno JL-300, фірмова стійка, телеметрія, стартовий набір інгредієнтів, навчання та запуск. "
            "Окремо зазвичай лишаються витрати, що залежать від вашої ситуації (наприклад, оренда локації чи електрика). "
            "Підкажіть місто/район і формат локації — і я підкажу, на що звернути увагу саме у вашому випадку."
        ),
        "payback": (
            "Без цифр справді немає сенсу рухатись далі. "
            "У базовій моделі середня маржа з чашки — близько 1,8 €, а типовий обсяг — приблизно 35 чашок на день. "
            "Це дає валову маржу близько 1 900 € на місяць, і після стандартних витрат часто залишається орієнтовно 1 200–1 300 € чистого результату. "
            "У середньому окупність виходить близько 9–12 місяців, але ключовий фактор — локація й потік людей. "
            "Скажіть, у вас локація вже є чи ви ще в пошуку?"
        ),
        "terms": (
            "Важливий момент — і тут часто бувають неправильні очікування. "
            "Maison de Café — це партнерська модель: ви інвестуєте в обладнання і керуєте точкою, "
            "а ми забезпечуємо продукт, стандарти якості, навчання і підтримку на старті. "
            "Далі найкраще перейти до вашої ситуації: де плануєте ставити точку і який формат локації розглядаєте?"
        ),
        "contacts": CONTACTS_TEXT["UA"],
    },
    "RU": {
        "what": (
            "Хороший вопрос — с него обычно и начинается знакомство. "
            "Maison de Café — это готовая точка самообслуживания «под ключ» в Бельгии: профессиональный автомат Jetinno JL-300, "
            "фирменная стойка, система контроля и стартовый набор ингредиентов, плюс обучение и сопровождение запуска. "
            "Формат рассчитан на быстрый старт без опыта и работу без персонала. "
            "Что вам удобнее дальше: разобрать стоимость запуска или сразу пройтись по окупаемости и цифрам?"
        ),
        "price": (
            "Это самый логичный вопрос, и тут важно говорить честно. "
            "Базовая стоимость запуска точки Maison de Café в Бельгии — 9 800 €. "
            "В сумму входит Jetinno JL-300, фирменная стойка, телеметрия, стартовый набор ингредиентов, обучение и запуск. "
            "Отдельно обычно остаются расходы, зависящие от вашей ситуации (например, аренда локации или электричество). "
            "Скажите город/район и тип места — и я подскажу, на что смотреть именно в вашем случае."
        ),
        "payback": (
            "Без цифр действительно нет смысла идти дальше. "
            "В базовой модели средняя маржа с чашки — около 1,8 €, а типичный объём — примерно 35 чашек в день. "
            "Это даёт валовую маржу около 1 900 € в месяц, и после стандартных расходов часто остаётся примерно 1 200–1 300 € чистого результата. "
            "В среднем окупаемость получается около 9–12 месяцев, но решающий фактор — локация и поток людей. "
            "У вас место уже есть или вы ещё в поиске?"
        ),
        "terms": (
            "Это важный момент — и здесь чаще всего ошибаются ожиданиями. "
            "Maison de Café — это партнёрская модель: вы инвестируете в оборудование и управляете точкой, "
            "а мы обеспечиваем продукт, стандарты качества, обучение и поддержку на старте. "
            "Давайте оттолкнёмся от вашей ситуации: где планируете ставить точку и какой формат локации рассматриваете?"
        ),
        "contacts": CONTACTS_TEXT["RU"],
    },
    "EN": {
        "what": (
            "Good question — it’s usually the starting point. "
            "Maison de Café is a turnkey self-service coffee point in Belgium: a Jetinno JL-300 machine, branded stand, control system, "
            "a starter set of ingredients, plus training and launch support. It’s designed for a fast start without prior coffee-business experience, "
            "and it works without staff. Would you like to discuss the opening cost next, or go straight to payback and numbers?"
        ),
        "price": (
            "That’s the most logical question, and it’s important to be transparent. "
            "The base launch cost for a Maison de Café point in Belgium is 9 800 €. "
            "It includes the Jetinno JL-300, branded stand, telemetry, starter ingredients, training, and launch support. "
            "Separate costs usually depend on your specific situation (for example, rent or electricity). "
            "Tell me the city/area and location type — and I’ll guide you on what matters most for your case."
        ),
        "payback": (
            "If we don’t understand the numbers, there’s no point moving forward. "
            "In the base model, the average margin per cup is about 1.8 €, and a typical volume is around 35 cups/day. "
            "That’s roughly 1 900 € gross margin per month, and after standard costs, it often leaves around 1 200–1 300 € net. "
            "Average payback is about 9–12 months, but the key factor is the location traffic. "
            "Do you already have a spot, or are you still searching?"
        ),
        "terms": (
            "This is an important point — expectations are often wrong here. "
            "Maison de Café is a partnership model: you invest in the equipment and manage the point, "
            "and we provide product, quality standards, training, and launch support. "
            "Let’s make it practical: what city/area and what type of location are you considering?"
        ),
        "contacts": CONTACTS_TEXT["EN"],
    },
    "FR": {
        "what": (
            "Bonne question — c’est souvent le point de départ. "
            "Maison de Café est un point café en libre-service « clé en main » en Belgique : une machine Jetinno JL-300, un stand de marque, "
            "un système de contrôle, un kit de démarrage d’ingrédients, plus formation et accompagnement au lancement. "
            "Le format est pensé pour démarrer vite, sans expérience, et fonctionner sans personnel. "
            "Vous préférez qu’on voie le coût de lancement ou directement la rentabilité et les chiffres ?"
        ),
        "price": (
            "C’est la question la plus logique, et il faut être transparent. "
            "Le coût de base pour lancer un point Maison de Café en Belgique est de 9 800 €. "
            "Cela inclut la Jetinno JL-300, le stand, la télémétrie, le kit d’ingrédients, la formation et le lancement. "
            "Certains coûts restent liés à votre situation (par exemple loyer ou électricité). "
            "Dites-moi la ville/quartier et le type d’emplacement — et je vous guide sur les points clés."
        ),
        "payback": (
            "Sans chiffres, ça n’a pas de sens d’aller plus loin. "
            "Dans le modèle de base, la marge moyenne par tasse est d’environ 1,8 €, et le volume typique est d’environ 35 tasses/jour. "
            "Cela fait environ 1 900 € de marge brute par mois, et après les coûts standards, il reste souvent autour de 1 200–1 300 € net. "
            "Le retour sur investissement est en moyenne de 9–12 mois, mais le facteur clé est le flux de l’emplacement. "
            "Vous avez déjà un lieu ou vous êtes encore en recherche ?"
        ),
        "terms": (
            "Point important — c’est là que les attentes se trompent le plus souvent. "
            "Maison de Café fonctionne en modèle partenaire : vous investissez dans l’équipement et vous gérez le point, "
            "et nous fournissons le produit, les standards qualité, la formation et l’accompagnement au démarrage. "
            "Pour avancer : vous visez quelle ville/quartier et quel type d’emplacement ?"
        ),
        "contacts": CONTACTS_TEXT["FR"],
    },
}


def gold_lang(lang: str) -> str:
    return lang if lang in GOLD else "UA"

# =========================
# Anti-legacy franchise content guard
# =========================
BANNED_PATTERNS = [
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
    r"\b1\s*500\s*[–-]\s*2\s*000\b",
    r"\bпаушальн",
    r"\bроялти\b",
    r"\bfranchise fee\b",
    r"\broyalt",
]
def looks_like_legacy_franchise(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)


_ALLOWED_NUMBER_PATTERNS = [
    r"\b9\s*800\b",
    r"\b9800\b",
    r"\b1[\.,]8\b",
    r"\b35\b",
    r"\b1\s*900\b",
    r"\b1900\b",
    r"\b1\s*200\b",
    r"\b1200\b",
    r"\b1\s*300\b",
    r"\b1300\b",
    r"\b9\s*[–-]\s*12\b",
]
def _has_disallowed_numbers(text: str) -> bool:
    if not text:
        return False
    tokens = re.findall(r"(?<!\w)(\d+[\d\s]*[\.,]?\d*)(?!\w)", text)
    if not tokens:
        return False
    tmp = text
    for p in _ALLOWED_NUMBER_PATTERNS:
        tmp = re.sub(p, "", tmp)
    return bool(re.search(r"\d", tmp))


async def ensure_thread(user: UserState) -> str:
    if user.thread_id:
        return user.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    user.thread_id = thread.id
    save_state()
    return thread.id


def _draft_instructions(lang: str) -> str:
    # concise, production-safe
    if lang == "UA":
        return (
            "Ти — Max, консультант Maison de Café. Відповідай по-людськи, спокійно, впевнено. "
            "Не згадуй бази знань/файли/пошук. "
            "НЕ вигадуй цифри, пакети, роялті, паушальні внески або шаблони «класичної франшизи». "
            "Якщо бракує деталей — поясни просто й задай 1 коротке уточнення. Завжди заверши м’яким наступним кроком."
        )
    if lang == "EN":
        return (
            "You are Max, a Maison de Café consultant. Speak naturally and confidently. "
            "Do not mention knowledge bases/files/search. "
            "Do NOT invent numbers, packages, royalties, franchise fees, or generic coffee-shop templates. "
            "If details are needed, explain simply and ask 1 short clarifying question. Always end with a soft next step."
        )
    if lang == "FR":
        return (
            "Tu es Max, consultant Maison de Café. Réponds de façon humaine et sûre. "
            "Ne mentionne pas de base de connaissances/fichiers/recherche. "
            "N’invente pas de chiffres, de packs, de royalties ou de « franchise classique ». "
            "Si des détails manquent, explique simplement et pose 1 question courte. Termine toujours par un prochain pas."
        )
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по-человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или шаблоны «классической франшизы». "
        "Если не хватает деталей — объясни просто и задай 1 короткий уточняющий вопрос. Всегда заканчивай мягким следующим шагом."
    )


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
        return {
            "UA": "Розумію. Щоб відповісти точніше: підкажіть місто/район і тип локації — тоді дам чіткий розбір.",
            "RU": "Понял. Чтобы ответить точнее: подскажите город/район и тип локации — и я дам чёткий разбор.",
            "EN": "Got it. To be precise: tell me the city/area and location type, and I’ll give a clear breakdown.",
            "FR": "Compris. Pour être précis : dites-moi la ville/quartier et le type d’emplacement, et je vous réponds clairement.",
        }.get(lang, "Ок, уточните пару деталей — и продолжим.")

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

Draft answer:
{draft}

Hard rules:
- Remove any mention or implication of: royalties, franchise fees/entry fees, classic franchise model, "we train your staff" as a requirement.
- Remove any numbers except: 9800, 9 800, 1.8 (1,8), 35, 1900 (1 900), 1200 (1 200), 1300 (1 300), 9–12.
- If you must remove numbers, rewrite the sentence without numbers.
- Output only the final user-facing answer, in the same language.
- Tone: Max (human, confident), end with a clear next step.
""".strip()

    # If draft already looks polluted -> verifier still rewrites, but we also allow fallback later
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
        gl = gold_lang(lang)
        return GOLD[gl]["what"]

    if looks_like_legacy_franchise(answer) or _has_disallowed_numbers(answer):
        gl = gold_lang(lang)
        q = (question or "").lower()

        if any(w in q for w in ["сколько", "скільки", "cost", "prix", "цена", "стоим"]):
            return GOLD[gl]["price"]
        if any(w in q for w in ["окуп", "окупн", "profit", "rentab", "прибыл", "прибут"]):
            return GOLD[gl]["payback"]
        if any(w in q for w in ["услов", "умов", "terms", "franch", "партнер"]):
            return GOLD[gl]["terms"]
        if any(w in q for w in ["контакт", "contacts", "контакти", "телефон", "email"]):
            return GOLD[gl]["contacts"]
        return GOLD[gl]["what"]

    return answer


async def ask_assistant(user_id: str, user_text: str, lang: str) -> str:
    draft = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang)
    fixed = await _verify_and_fix(question=user_text, draft=draft, lang=lang)
    return _final_safety_override(question=user_text, answer=fixed, lang=lang)


# =========================
# VOICE -> TRANSCRIBE
# =========================
async def transcribe_voice_ogg(file_path: str) -> str:
    """
    Uses OpenAI audio transcription.
    Returns plain text.
    """
    try:
        with open(file_path, "rb") as f:
            tr = await asyncio.to_thread(
                client.audio.transcriptions.create,
                model=TRANSCRIBE_MODEL,
                file=f,
            )
        text = (getattr(tr, "text", None) or "").strip()
        if text:
            return text
    except Exception as e:
        log.warning("Transcribe failed with %s: %s", TRANSCRIBE_MODEL, e)

    # fallback
    try:
        with open(file_path, "rb") as f:
            tr = await asyncio.to_thread(
                client.audio.transcriptions.create,
                model="whisper-1",
                file=f,
            )
        return (getattr(tr, "text", None) or "").strip()
    except Exception as e:
        log.warning("Fallback transcribe failed: %s", e)
        return ""

# =========================
# BUTTON TEXT ROUTING (reply keyboard)
# =========================
def _is_button(text: str, lang: str, key: str) -> bool:
    L = MENU.get(lang, MENU["UA"])
    return (text or "").strip() == L.get(key, "")


async def _send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass


# =========================
# COMMANDS / HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    txt = {
        "UA": "Привіт! Я Max, консультант Maison de Café. Оберіть пункт меню або просто напишіть питання — я відповім по суті.",
        "RU": "Привет! Я Max, консультант Maison de Café. Выберите пункт меню или просто задайте вопрос — отвечу по сути.",
        "EN": "Hi! I’m Max, Maison de Café consultant. Choose a menu item or just ask a question — I’ll answer clearly.",
        "FR": "Bonjour ! Je suis Max, consultant Maison de Café. Choisissez un пункт du menu ou posez votre question — je réponds clairement.",
    }.get(u.lang, "Hi!")

    # Show reply keyboard ONLY here
    await update.message.reply_text(txt, reply_markup=reply_menu(u.lang))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if OWNER_TELEGRAM_ID and user_id != OWNER_TELEGRAM_ID:
        return
    await update.message.reply_text(
        f"Users: {len(_state)}\nBlocked: {len(_blocked)}\nAssistant: {ASSISTANT_ID}\nToken: {mask_token(TELEGRAM_BOT_TOKEN)}\nPresentation: {'set' if PRESENTATION_FILE_ID else 'missing'}"
    )


async def on_language_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Reply-button "Language/Мова/Язык" -> show inline language chooser.
    IMPORTANT: we do NOT attach reply keyboard here; it's already available if visible.
    """
    user_id = str(update.effective_user.id)
    u = get_user(user_id)

    txt = {
        "UA": "Оберіть мову:",
        "RU": "Выберите язык:",
        "EN": "Choose language:",
        "FR": "Choisissez la langue:",
    }.get(u.lang, "Choose language:")

    # Inline menu is ONLY for language selection
    await update.message.reply_text(txt, reply_markup=lang_inline_kb())


async def on_callback_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    if lang in LANGS:
        u.lang = lang
        save_state()

    txt = {
        "UA": "Мову змінено. Оберіть пункт меню або задайте питання.",
        "RU": "Язык изменён. Выберите пункт меню или задайте вопрос.",
        "EN": "Language updated. Choose a menu item or ask a question.",
        "FR": "Langue mise à jour. Choisissez un пункт du menu ou posez votre question.",
    }.get(u.lang, "OK")

    # After language change: show reply keyboard ONCE (so labels update)
    await q.message.reply_text(txt, reply_markup=reply_menu(u.lang))


async def _handle_presentation(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    if PRESENTATION_FILE_ID:
        try:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=PRESENTATION_FILE_ID)
            # Hide keyboard after action (UX requirement)
            await update.message.reply_text(
                {"UA":"Якщо хочете — напишіть, яку локацію розглядаєте, і я підкажу по окупності.",
                 "RU":"Если хотите — напишите, какую локацию рассматриваете, и я подскажу по окупаемости.",
                 "EN":"If you want, tell me your location type and I’ll guide you on payback.",
                 "FR":"Si vous voulez, dites-moi votre type d’emplacement et je vous guide sur la rentabilité."}.get(lang, "OK"),
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        except Exception as e:
            log.warning("Presentation send failed: %s", e)

    await update.message.reply_text(
        {
            "UA": "Презентація ще не підключена. Як тільки додамо файл — одразу зможу надіслати.",
            "RU": "Презентация ещё не подключена. Как только добавим файл — сразу смогу отправить.",
            "EN": "The presentation isn’t connected yet. As soon as we add the file, I can send it.",
            "FR": "La présentation n’est pas encore connectée. Dès que le fichier est ajouté, je peux l’envoyer.",
        }.get(lang, "Presentation not connected."),
        reply_markup=ReplyKeyboardRemove(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    lang = u.lang
    text = (update.message.text or "").strip()
    if not text:
        return

    # 1) Reply-menu buttons (Gold / Contacts / Presentation / Language)
    if _is_button(text, lang, "b_lang"):
        await on_language_button(update, context)
        # Do NOT force keyboard; user can keep it open if visible
        return

    if _is_button(text, lang, "b_presentation"):
        await _handle_presentation(update, context, lang)
        return

    if _is_button(text, lang, "b_what"):
        gl = gold_lang(lang)
        await update.message.reply_text(GOLD[gl]["what"], reply_markup=ReplyKeyboardRemove())
        return

    if _is_button(text, lang, "b_price"):
        gl = gold_lang(lang)
        await update.message.reply_text(GOLD[gl]["price"], reply_markup=ReplyKeyboardRemove())
        return

    if _is_button(text, lang, "b_payback"):
        gl = gold_lang(lang)
        await update.message.reply_text(GOLD[gl]["payback"], reply_markup=ReplyKeyboardRemove())
        return

    if _is_button(text, lang, "b_terms"):
        gl = gold_lang(lang)
        await update.message.reply_text(GOLD[gl]["terms"], reply_markup=ReplyKeyboardRemove())
        return

    if _is_button(text, lang, "b_contacts"):
        gl = gold_lang(lang)
        await update.message.reply_text(GOLD[gl]["contacts"], reply_markup=ReplyKeyboardRemove())
        return

    # 2) Normal user text -> 2-pass assistant
    await _send_typing(context, update.effective_chat.id)
    ans = await ask_assistant(user_id=user_id, user_text=text, lang=lang)

    # IMPORTANT UX: do NOT attach reply keyboard here; hide it
    await update.message.reply_text(ans, reply_markup=ReplyKeyboardRemove())


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return

    u = get_user(user_id)
    lang = u.lang

    voice = update.message.voice
    if not voice:
        return

    await _send_typing(context, update.effective_chat.id)

    # Download voice file
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        tmp_dir = Path("/tmp/maisondecafe_voice")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        local_path = str(tmp_dir / f"{voice.file_unique_id}.ogg")
        await tg_file.download_to_drive(custom_path=local_path)
    except Exception as e:
        log.warning("Voice download failed: %s", e)
        await update.message.reply_text(
            {"UA":"Не зміг прочитати голосове. Напишіть текстом — і я відповім одразу.",
             "RU":"Не получилось прочитать голосовое. Напишите текстом — отвечу сразу.",
             "EN":"I couldn’t read the voice message. Please type it and I’ll answer right away.",
             "FR":"Je n’ai pas pu lire le message vocal. Écrivez-le et je réponds tout de suite."}.get(lang, "Please type it."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    text = await transcribe_voice_ogg(local_path)
    if not text:
        await update.message.reply_text(
            {"UA":"Я не розпізнав голосове. Спробуйте ще раз або напишіть текстом.",
             "RU":"Не удалось распознать голосовое. Попробуйте ещё раз или напишите текстом.",
             "EN":"I couldn’t transcribe it. Please try again or type the message.",
             "FR":"Je n’ai pas pu transcrire. Réessayez ou écrivez le message."}.get(lang, "Try again."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Use transcribed text as normal pipeline input
    await _send_typing(context, update.effective_chat.id)
    ans = await ask_assistant(user_id=user_id, user_text=text, lang=lang)
    await update.message.reply_text(ans, reply_markup=ReplyKeyboardRemove())


# =========================
# Polling safety: clear webhook to avoid conflict
# =========================
async def post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared (drop_pending_updates=True)")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)


def build_app() -> Application:
    return ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()


def main() -> None:
    acquire_singleton_lock_or_exit()
    load_state()

    app = build_app()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline language chooser callback
    app.add_handler(CallbackQueryHandler(on_callback_lang, pattern=r"^l:(UA|RU|EN|FR)$"))

    # Voice messages
    app.add_handler(MessageHandler(filters.VOICE, on_voice))

    # Text (non-commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Polling
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
