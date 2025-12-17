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

        ai_reply, fs_used = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=user_text, action_key=None)

        if looks_like_kb_missing(ai_reply, lang, fs_used):
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

    # lead flow priority
    if user_id in lead_states:
        handled = await handle_lead_form(update, context)
        if handled:
            return

    # language menu
    if is_language_button(text):
        await show_language_menu(update, context)
        return

    chosen = parse_lang_choice(text)
    if chosen:
        await set_language(update, context, chosen)
        return

    action = button_action_from_text(text)

    # contacts static
    if is_contacts_button(text):
        lang = get_lang(user_id)
        await update.message.reply_text(TEXTS[lang]["contacts_text"], reply_markup=mk_main_keyboard(lang))
        return

    # lead form local logic
    if is_lead_button(text):
        await start_lead_form(update, context)
        return

    # content buttons: hard language binding
    if action and action[0] in {"what", "price", "payback", "franchise"}:
        action_key, button_lang = action
        user_lang[user_id] = button_lang

        command_text = f"[BUTTON:{action_key}] {MENU[button_lang][action_key]}"

        ai_reply, fs_used = await ask_assistant_strict(
            user_id=user_id,
            lang=button_lang,
            user_text=command_text,
            action_key=action_key,
        )

        if looks_like_kb_missing(ai_reply, button_lang, fs_used):
            await update.message.reply_text(TEXTS[button_lang]["kb_missing"], reply_markup=mk_main_keyboard(button_lang))
            return

        await update.message.reply_text(ai_reply, reply_markup=mk_main_keyboard(button_lang))
        return

    # normal question
    lang = get_lang(user_id)
    try:
        ai_reply, fs_used = await ask_assistant_strict(user_id=user_id, lang=lang, user_text=text, action_key=None)

        if looks_like_kb_missing(ai_reply, lang, fs_used):
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

    # non-text block
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
