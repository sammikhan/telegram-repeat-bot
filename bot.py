import asyncio
import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Iltimos yozing:\n/add Bugun o‘rgangan mavzu")
        return

    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    now = datetime.now()

    for days in [3, 7, 30]:
        run_time = now + timedelta(days=days)
        context.job_queue.run_once(
            remind,
            when=run_time,
            chat_id=chat_id,
            data=text
        )

    await update.message.reply_text(
        f"✅ Saqlandi!\n📌 {text}\n⏰ 3, 7, 30 kunda eslatadi."
    )

async def remind(context: ContextTypes.DEFAULT_TYPE):
    text = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"🔁 Takrorlash vaqti keldi:\n📘 {text}"
    )

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Render Environment’da qo‘shing.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("add", add))

    print("Bot ishga tushdi...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
