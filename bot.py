message.text
        lead_states[uid] = "message"
        await update.message.reply_text("Коротко опишите ваш запрос")
    elif state == "message":
        lead_data[uid]["message"] = update.message.text
        lead_states.pop(uid)

        text = (
            f"🔔 Новый лид Maison de Café\n\n"
            f"Имя: {lead_data[uid]['name']}\n"
            f"Телефон: {lead_data[uid]['phone']}\n"
            f"Email: {lead_data[uid]['email']}\n"
            f"Сообщение: {lead_data[uid]['message']}"
        )

        if OWNER_TELEGRAM_ID:
            await context.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=text)

        await update.message.reply_text(
            "Спасибо! Менеджер свяжется с вами в течение 24 часов.",
            reply_markup=MAIN_KEYBOARD,
        )

# ====== ТЕКСТ ======
async def process_text(update, context, text):
    user_id = str(update.effective_user.id)

    if user_id not in user_languages:
        await update.message.reply_text(
            "Пожалуйста, выберите язык:",
            reply_markup=LANGUAGE_KEYBOARD,
        )
        return

    if "контакт" in text.lower():
        await update.message.reply_text(CONTACT_TEXT)
        return

    if "заявк" in text.lower():
        await start_lead(update, context)
        return

    thread_id = user_threads[user_id]

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=text,
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
    )

    while True:
        status = client.beta.threads.runs.retrieve(thread_id, run.id)
        if status.status == "completed":
            break
        await asyncio.sleep(1)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    reply = messages.data[0].content[0].text.value

    await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)

# ====== ROUTER ======
async def handle_text(update, context):
    if await handle_language(update, context):
        return

    uid = update.effective_user.id
    if uid in lead_states:
        await handle_lead(update, context)
        return

    await process_text(update, context, update.message.text)

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()

if name == "__main__":
    main()
