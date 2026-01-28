import os
import asyncio
import asyncpg
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

REMIND_DAYS = [1, 3, 7, 30]


# ---------------- DB ----------------

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            remind_at TIMESTAMP NOT NULL,
            sent BOOLEAN DEFAULT FALSE
        );
    """)
    await conn.close()


async def add_reminder(chat_id: int, text: str, remind_at: datetime):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "INSERT INTO reminders(chat_id, text, remind_at) VALUES ($1, $2, $3)",
        chat_id, text, remind_at
    )
    await conn.close()


async def get_pending_reminders():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT id, chat_id, text, remind_at FROM reminders WHERE sent = FALSE"
    )
    await conn.close()
    return rows


async def mark_sent(reminder_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(
        "UPDATE reminders SET sent = TRUE WHERE id = $1",
        reminder_id
    )
    await conn.close()


# ---------------- BOT ----------------

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "✍️ Yozing:\n/add Bugun o‘rgangan mavzu"
        )
        return

    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    now = datetime.utcnow()

    for d in REMIND_DAYS:
        remind_at = now + timedelta(days=d)
        await add_reminder(chat_id, text, remind_at)

    await update.message.reply_text(
        f"✅ Saqlandi!\n\n📌 {text}\n\n⏰ Eslatma: 1 / 3 / 7 / 30 kunda"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT text, remind_at
        FROM reminders
        WHERE sent = FALSE AND chat_id = $1
        ORDER BY remind_at
    """, update.effective_chat.id)
    await conn.close()

    if not rows:
        await update.message.reply_text("📭 Aktiv eslatmalar yo‘q.")
        return

    msg = "📋 Aktiv eslatmalar:\n\n"
    now = datetime.utcnow()

    for r in rows:
        days_left = (r["remind_at"] - now).days
        msg += f"• {r['text']} — ⏳ {days_left} kun qoldi\n"

    await update.message.reply_text(msg)


# ---------------- SCHEDULER ----------------

async def reminder_worker(app):
    while True:
        conn = await asyncpg.connect(DATABASE_URL)
        rows = await conn.fetch("""
            SELECT id, chat_id, text
            FROM reminders
            WHERE sent = FALSE AND remind_at <= NOW()
        """)
        await conn.close()

        for r in rows:
            try:
                await app.bot.send_message(
                    chat_id=r["chat_id"],
                    text=f"⏰ Takrorlash vaqti!\n\n📌 {r['text']}"
                )
                await mark_sent(r["id"])
            except Exception as e:
                print("Send error:", e)

        await asyncio.sleep(30)


# ---------------- MAIN ----------------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q (Railway Variables’da qo‘shing)")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q (Postgres ulanmagan)")

    await init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))

    asyncio.create_task(reminder_worker(app))

    print("🤖 Bot ishga tushdi...")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
