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
