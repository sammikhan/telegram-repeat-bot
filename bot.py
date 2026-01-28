import os
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


# ---------- DATABASE ----------

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    conn = await get_conn()
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        remind_at TIMESTAMPTZ NOT NULL,
        sent BOOLEAN DEFAULT FALSE
    );
    """)
    await conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_reminders_time
    ON reminders(chat_id, sent, remind_at);
    """)
    await conn.close()


async def add_reminders(chat_id: int, text: str):
    now = datetime.now(timezone.utc)

    days_list = [1, 3, 7, 30]

    conn = await get_conn()

    for d in days_list:
        remind_at = now + timedelta(days=d)
        await conn.execute(
            "INSERT INTO reminders (chat_id, text, remind_at) VALUES ($1, $2, $3)",
            chat_id,
            text,
            remind_at,
        )

    await conn.close()


async def get_upcoming(chat_id: int):
    conn = await get_conn()
    rows = await conn.fetch("""
        SELECT id, text, remind_at
        FROM reminders
        WHERE chat_id=$1 AND sent=FALSE
        ORDER BY remind_at
    """, chat_id)
    await conn.close()
    return rows


async def mark_sent(rid: int):
    conn = await get_conn()
    await conn.execute("UPDATE reminders SET sent=TRUE WHERE id=$1", rid)
    await conn.close()


# ---------- BOT COMMANDS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Salom!\n\n"
        "✍️ Yangi mavzu qo‘shish:\n"
        "/add Matn\n\n"
        "📋 Rejalashtirilganlar:\n"
        "/list"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Iltimos: /add mavzu yozing")
        return

    text = " ".join(context.args)
    chat_id = update.effective_chat.id

    try:
        await add_reminders(chat_id, text)
        await update.message.reply_text(
            f"✅ Saqlandi!\n\n"
            f"🔁 Takrorlash: 1 / 3 / 7 / 30 kun"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {e}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await get_upcoming(chat_id)

    if not rows:
        await update.message.reply_text("📭 Rejalashtirilganlar yo‘q.")
        return

    msg = "📋 Yaqin takrorlashlar:\n\n"
    now = datetime.now(timezone.utc)

    for r in rows:
        days_left = (r["remind_at"] - now).days
        msg += f"• {r['text']} — ⏳ {days_left} kun\n"

    await update.message.reply_text(msg)


# ---------- REMINDER WORKER ----------

async def reminder_worker(app):
    while True:
        try:
            conn = await get_conn()
            now = datetime.now(timezone.utc)

            rows = await conn.fetch("""
                SELECT id, chat_id, text
                FROM reminders
                WHERE sent=FALSE AND remind_at <= $1
            """, now)

            for r in rows:
                await app.bot.send_message(
                    chat_id=r["chat_id"],
                    text=f"⏰ Takrorlash vaqti:\n\n{r['text']}"
                )
                await conn.execute("UPDATE reminders SET sent=TRUE WHERE id=$1", r["id"])

            await conn.close()

        except Exception as e:
            print("Worker error:", e)

        await asyncio.sleep(60)


# ---------- MAIN ----------

async def post_init(app):
    await init_db()
    app.create_task(reminder_worker(app))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
