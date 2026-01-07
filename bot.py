import os
import re
import json
import time
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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
PRESENTATION_FILE_ID = os.getenv("PRESENTATION_FILE_ID", "").strip()  # Telegram file_id for the presentation PDF

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
# STATE (persisted)
# =========================
STATE_FILE = Path("healthbot_state.json")


@dataclass
class UserState:
    lang: str = "UA"       # UA/RU/EN/FR
    thread_id: str = ""    # per-user shared thread (we keep it simple & stable)


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

MENU_LABELS = {
    "UA": {
        "what": "☕ Що таке Maison de Café?",
        "price": "💶 Скільки коштує відкрити?",
        "payback": "📈 Окупність і прибуток",
        "terms": "🤝 Умови співпраці",
        "contacts": "📞 Контакти",
        "lead": "📝 Залишити заявку",
        "presentation": "📄 Презентація",
        "lang": "🌍 Мова",
    },
    "RU": {
        "what": "☕ Что такое Maison de Café?",
        "price": "💶 Сколько стоит открыть?",
        "payback": "📈 Окупаемость и прибыль",
        "terms": "🤝 Условия сотрудничества",
        "contacts": "📞 Контакты",
        "lead": "📝 Оставить заявку",
        "presentation": "📄 Презентация",
        "lang": "🌍 Язык",
    },
    "EN": {
        "what": "☕ What is Maison de Café?",
        "price": "💶 Opening cost",
        "payback": "📈 Payback & profit",
        "terms": "🤝 Partnership terms",
        "contacts": "📞 Contacts",
        "lead": "📝 Leave a request",
        "presentation": "📄 Presentation",
        "lang": "🌍 Language",
    },
    "FR": {
        "what": "☕ Qu’est-ce que Maison de Café ?",
        "price": "💶 Coût de lancement",
        "payback": "📈 Rentabilité & profit",
        "terms": "🤝 Conditions",
        "contacts": "📞 Contacts",
        "lead": "📝 Laisser une demande",
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

# GOLD answers (button-safe). Keep facts consistent and minimal.
GOLD = {
    "UA": {
        "what": (
            "Хороший запит — з цього зазвичай і починається знайомство. "
            "Maison de Café — це готова точка самообслуговування «під ключ» у Бельгії: професійний автомат Jetinno JL-300, "
            "фірмова стійка, система контролю та стартовий набір інгредієнтів, плюс навчання і супровід запуску. "
            "Формат розрахований на швидкий старт без досвіду та роботу без персоналу. "
            "Як вам зручніше далі: розібрати вартість запуску чи одразу пройтися по окупності й цифрах?"
        ),
        "price": (
            "Це найлогічніше питання — і тут важливо говорити чесно. "
            "Базова вартість запуску точки Maison de Café в Бельгії — 9 800 €. "
            "У цю суму входить Jetinno JL-300, фірмова стійка, телеметрія, стартовий набір інгредієнтів, навчання та запуск. "
            "Окремо зазвичай лишаються витрати, що залежать від вашої ситуації (наприклад, оренда локації чи електрика). "
            "Хочете — підкажіть місто/район і формат локації, і я підкажу, на що звернути увагу саме у вашому випадку."
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
            "Хотите — скажите город/район и тип места, и я подскажу, на что смотреть именно в вашем случае."
        ),
        "payback": (
            "Без цифр действительно нет смысла идти дальше. "
            "В базовой модели средняя маржа с чашки — около 1,8 €, а типичный объём — примерно 35 чашек в день. "
            "Это даёт валовую маржу около 1 900 € в месяц, и после стандартных расходов часто остаётся примерно 1 200–1 300 € чистого результата. "
            "В среднем окупаемость получается около 9–12 месяцев, но решающий фактор — локация и поток людей. "
            "Скажите, у вас место уже есть или вы ещё в поиске?"
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
            "Dans le modèle de base, la marge moyenne par tasse est d’environ 1,8 €, et le volume типique est d’environ 35 tasses/jour. "
            "Cela fait environ 1 900 € de marge brute par mois, et après les coûts стандарт, il reste souvent autour de 1 200–1 300 € net. "
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


def main_keyboard(lang: str) -> InlineKeyboardMarkup:
    L = MENU_LABELS.get(lang, MENU_LABELS["UA"])
    kb = [
        [InlineKeyboardButton(L["what"], callback_data="m:what")],
        [InlineKeyboardButton(L["price"], callback_data="m:price")],
        [InlineKeyboardButton(L["payback"], callback_data="m:payback")],
        [InlineKeyboardButton(L["terms"], callback_data="m:terms")],
        [InlineKeyboardButton(L["contacts"], callback_data="m:contacts")],
        [InlineKeyboardButton(L["lead"], callback_data="m:lead")],
        [InlineKeyboardButton(L["presentation"], callback_data="m:presentation")],
        [InlineKeyboardButton(L["lang"], callback_data="m:lang")],
    ]
    return InlineKeyboardMarkup(kb)


def lang_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(LANG_LABELS["UA"], callback_data="l:UA"),
         InlineKeyboardButton(LANG_LABELS["RU"], callback_data="l:RU")],
        [InlineKeyboardButton(LANG_LABELS["EN"], callback_data="l:EN"),
         InlineKeyboardButton(LANG_LABELS["FR"], callback_data="l:FR")],
    ]
    return InlineKeyboardMarkup(kb)


# =========================
# Sanity guard: ban old/incorrect franchise template content
# =========================
BANNED_PATTERNS = [
    r"\b49\s*000\b",
    r"\b55\s*000\b",
    r"\b150\s*000\b",
    r"\b1\s*500\s*[–-]\s*2\s*000\b",
    r"\bпаушальн",
    r"\bроялти\b",
]
def looks_like_legacy_franchise(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in BANNED_PATTERNS)

async def ensure_thread(user: UserState) -> str:
    if user.thread_id:
        return user.thread_id
    thread = await asyncio.to_thread(client.beta.threads.create)
    user.thread_id = thread.id
    save_state()
    return thread.id


# =========================
# ANSWER PIPELINE (2-PASS): DRAFT -> VERIFY -> SEND
# =========================

VERIFY_MODEL = os.getenv("VERIFY_MODEL", "gpt-4o-mini").strip()  # used for 2nd-pass verification (no KB access)

# Allowed numeric facts (Gold / Master). Any other numbers are removed or require clarification.
_ALLOWED_NUMBER_PATTERNS = [
    r"\b9\s*800\b",          # 9 800
    r"\b9800\b",              # 9800
    r"\b1[\.,]8\b",          # 1.8 / 1,8
    r"\b35\b",                # 35 cups/day
    r"\b1\s*900\b",          # 1 900
    r"\b1200\b",
    r"\b1\s*200\b",
    r"\b1300\b",
    r"\b1\s*300\b",
    r"\b9\s*[–-]\s*12\b",   # 9–12
]

def _has_disallowed_numbers(text: str) -> bool:
    """Return True if text contains numbers outside the allowed set."""
    if not text:
        return False
    # Find number-like tokens
    tokens = re.findall(r"(?<!\w)(\d+[\d\s]*[\.,]?\d*)(?!\w)", text)
    if not tokens:
        return False
    # Remove allowed patterns first
    tmp = text
    for p in _ALLOWED_NUMBER_PATTERNS:
        tmp = re.sub(p, "", tmp)
    # After removing allowed patterns, if still contains digits -> disallowed
    return bool(re.search(r"\d", tmp))

def _draft_instructions(lang: str) -> str:
    """Short, production-safe instruction set for the Assistant run (File Search enabled)."""
    # Keep this concise to reduce instruction overflow.
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
    # RU default
    return (
        "Ты — Max, консультант Maison de Café. Отвечай по-человечески, спокойно, уверенно. "
        "Не упоминай базы знаний/файлы/поиск. "
        "НЕ придумывай цифры, пакеты, роялти, паушальные взносы или шаблоны «классической франшизы». "
        "Если для точного ответа не хватает данных — объясни это просто и задай 1 короткий уточняющий вопрос."
    )

async def _assistant_draft(user_id: str, user_text: str, lang: str) -> str:
    """PASS 1: KB-enabled draft from the configured Assistant."""
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
            "UA": "Розумію. Щоб відповісти точніше, підкажіть, будь ласка: яка локація (місто/район) і який у вас орієнтир по бюджету?",
            "RU": "Понял. Чтобы ответить точнее, подскажите, пожалуйста: какая локация (город/район) и какой у вас ориентир по бюджету?",
            "EN": "Got it. To answer precisely: what city/area is the location, and what budget range are you considering?",
            "FR": "Compris. Pour répondre précisément : quelle ville/quartier et quel budget envisagez-vous ?",
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
    """PASS 2: Compliance-style verification + rewrite (no KB access)."""
    # Fast path: if draft already hits banned patterns, go straight to GOLD fallback.
    if looks_like_legacy_franchise(draft) or _has_disallowed_numbers(draft):
        # Verifier will rewrite to remove forbidden content and numbers.
        pass

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
- Remove any mention or implication of: royalties, franchise fees/entry fees, staff training as a requirement, mandatory suppliers, classic coffee shop formats (island/pavilion) unless explicitly asked and clearly supported.
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
                {"role": "user", "content": user},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or draft
    except Exception as e:
        log.warning("Verifier failed: %s", e)
        return draft

def _final_safety_override(question: str, answer: str, lang: str) -> str:
    """Last guardrail: if still polluted, fall back to GOLD for the closest intent."""
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
        return GOLD[gl]["what"]
    return answer

async def ask_assistant(user_id: str, user_text: str, lang: str) -> str:
    """Public API used by message handlers: 2-pass answer with final guardrails."""
    draft = await _assistant_draft(user_id=user_id, user_text=user_text, lang=lang)
    fixed = await _verify_and_fix(question=user_text, draft=draft, lang=lang)
    final = _final_safety_override(question=user_text, answer=fixed, lang=lang)
    return final


# =========================
# COMMANDS / HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    u = get_user(user_id)
    await update.message.reply_text(
        {
            "UA": "Привіт! Я Max, консультант Maison de Café. Оберіть пункт меню — і я підкажу по суті.",
            "RU": "Привет! Я Max, консультант Maison de Café. Выберите пункт меню — и я подскажу по сути.",
            "EN": "Hi! I’m Max, Maison de Café consultant. Choose a menu item and I’ll guide you.",
            "FR": "Bonjour ! Je suis Max, consultant Maison de Café. Choisissez un пункт du menu et je vous guide.",
        }.get(u.lang, "Hi!"),
        reply_markup=main_keyboard(u.lang),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if OWNER_TELEGRAM_ID and user_id != OWNER_TELEGRAM_ID:
        return
    await update.message.reply_text(
        f"Users: {len(_state)}\nBlocked: {len(_blocked)}\nAssistant: {ASSISTANT_ID}\nToken: {mask_token(TELEGRAM_BOT_TOKEN)}"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = str(q.from_user.id)
    if user_id in _blocked:
        return
    u = get_user(user_id)
    data = q.data or ""

    if data.startswith("l:"):
        lang = data.split(":", 1)[1]
        if lang in LANGS:
            u.lang = lang
            save_state()
        # IMPORTANT: do NOT re-attach main keyboard here to avoid "buttons after every message"
        await q.message.reply_text(
            {"UA":"Мову змінено.","RU":"Язык изменён.","EN":"Language updated.","FR":"Langue mise à jour."}.get(u.lang, "OK")
        )
        return

    if data == "m:lang":
        # Show language picker (this is expected to have buttons)
        await q.message.reply_text(
            {"UA":"Оберіть мову:","RU":"Выберите язык:","EN":"Choose language:","FR":"Choisissez la langue:"}.get(u.lang, "Choose language:"),
            reply_markup=lang_keyboard(),
        )
        return

    if data.startswith("m:"):
        key = data.split(":", 1)[1]

        if key == "presentation":
            if PRESENTATION_FILE_ID:
                try:
                    await context.bot.send_document(chat_id=q.message.chat_id, document=PRESENTATION_FILE_ID)
                except Exception as e:
                    log.warning("Presentation send failed: %s", e)
                    await q.message.reply_text(
                        {"UA":"Не зміг відправити презентацію. Напишіть мені — і я надішлю її іншим способом.",
                         "RU":"Не получилось отправить презентацию. Напишите мне — и я пришлю другим способом.",
                         "EN":"I couldn't send the presentation file here. Message me and I’ll share it another way.",
                         "FR":"Je n’arrive pas à envoyer la présentation ici. Écrivez-moi et je la partagerai autrement."}.get(u.lang, "Couldn't send the presentation.")
                    )
            else:
                await q.message.reply_text(
                    {"UA":"Презентація ще не підключена. Я можу надіслати її, як тільки ми додамо файл.",
                     "RU":"Презентация ещё не подключена. Могу отправить, как только добавим файл.",
                     "EN":"The presentation is not connected yet. I can send it as soon as we add the file.",
                     "FR":"La présentation n’est pas encore connectée. Je peux l’envoyer dès que le fichier est ajouté."}.get(u.lang, "Presentation not connected yet.")
                )
            return

        if key in ("what", "price", "payback", "terms", "contacts"):
            gl = gold_lang(u.lang)
            # IMPORTANT: no main_keyboard here -> keyboard remains only on the /start message
            await q.message.reply_text(GOLD[gl][key])
            return
        if key == "lead":
            txt = {
                "UA": "Ок, давайте коротко. Напишіть: 1) місто/район, 2) тип локації, 3) місце вже є чи ви в пошуку.",
                "RU": "Ок, давайте коротко. Напишите: 1) город/район, 2) тип локации, 3) место уже есть или вы в поиске.",
                "EN": "Great. Please tell me: 1) city/area, 2) location type, 3) do you already have a spot or still searching?",
                "FR": "Très bien. Dites-moi : 1) ville/quartier, 2) type d’emplacement, 3) vous avez déjà un lieu ou vous cherchez ?",
            }.get(u.lang, "Ок, уточните детали.")
            await q.message.reply_text(txt)
            return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if user_id in _blocked:
        return
    u = get_user(user_id)
    text = (update.message.text or "").strip()
    if not text:
        return

    ans = await ask_assistant(user_id=user_id, user_text=text, lang=u.lang)

    # IMPORTANT: no keyboard on every answer (menu stays only on /start message)
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
    load_state()
    app = build_app()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
