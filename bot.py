import os
import asyncio
import asyncpg
from datetime import datetime, timedelta, timezone

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
            remind_at TIMESTAMPTZ NOT NULL,
            sent BOOLEAN DEFAULT FALSE
        );
    """)
    await conn.close()


async def add_reminders(chat_id: int, text: str):
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        for d in REMIND_DAYS:
            remind_at = now + timedelta(days=d)
            await conn.execute(
                "INSERT INTO reminders(chat_id, text, remind_at) VALUES ($1, $2, $3)",
                chat_id, text, remind_at
            )
    finally:
        await conn.close()


async def list_pending(chat_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT text, remind_at
            FROM reminders
            WHERE sent = FALSE AND chat_id = $1
            ORDER BY remind_at
        """, chat_id)
        return rows
    finally:
        await conn.close()


async def fetch_due():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT id, chat_id, text
            FROM reminders
            WHERE sent = FALSE AND remind_at <= NOW()
            ORDER BY remind_at
            LIMIT 50
        """)
        return rows
    finally:
        await conn.close()


async def mark_sent(reminder_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("UPDATE reminders SET sent = TRUE WHERE id = $1", reminder_id)
    finally:
        await conn.close()


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! 👋\n\n"
        "Mavzu qo‘shish: /add mavzu\n"
        "Ro‘yxat: /list\n\n"
        "Eslatma: 1 / 3 / 7 / 30 kun"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("✍️ Yozing:\n/add Bugun o‘rgangan mavzu")
        return

    text = " ".join(context.args)
    chat_id = update.effective_chat.id

    await add_reminders(chat_id, text)

    await update.message.reply_text(
        f"✅ Saqlandi!\n\n📌 {text}\n\n⏰ Eslatma: 1 / 3 / 7 / 30 kunda"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await list_pending(chat_id)

    if not rows:
        await update.message.reply_text("📭 Aktiv eslatmalar yo‘q.")
        return

    now = datetime.now(timezone.utc)
    msg = "📋 Aktiv eslatmalar:\n\n"

    # bir xil text bo‘yicha eng yaqin remind_at ni ko‘rsatamiz (chiroyli bo‘lishi uchun)
    # (chunki bitta mavzu 4 ta remind bo‘ladi)
    from collections import defaultdict
    nearest = defaultdict(lambda: None)

    for r in rows:
        t = r["text"]
        ra = r["remind_at"]
        if nearest[t] is None or ra < nearest[t]:
            nearest[t] = ra

    for t, ra in nearest.items():
        days_left = max(0, int((ra - now).total_seconds() // 86400))
        msg += f"• {t}\n   ⏳ {days_left} kun qoldi (eng yaqin)\n"

    msg += "\nℹ️ Har mavzu uchun 1/3/7/30 kunda 4 ta eslatma bo‘ladi."
    await update.message.reply_text(msg)


# ---------------- WORKER ----------------

async def reminder_worker(app):
    # abadiy loop: vaqti kelganlarini yuboradi
    while True:
        try:
            due = await fetch_due()
            for r in due:
                try:
                    await app.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"⏰ Takrorlash vaqti!\n\n📌 {r['text']}"
                    )
                    await mark_sent(r["id"])
                except Exception as e:
                    print("Send error:", e)
        except Exception as e:
            print("Worker error:", e)

        await asyncio.sleep(30)


async def post_init(app):
    # DB tayyorlab, worker'ni bir marta start qilamiz
    await init_db()
    app.create_task(reminder_worker(app))


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q (Railway Variables’da qo‘shing)")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q (Postgres ulanmagan)")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
