import os
import asyncio
import asyncpg
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

# 1 / 3 / 7 / 30
REMIND_DAYS = [1, 3, 7, 30]


# ---------------- DB ----------------

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                remind_at TIMESTAMPTZ NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT FALSE
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat_sent_time ON reminders(chat_id, sent, remind_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_sent_time ON reminders(sent, remind_at);")
    finally:
        await conn.close()


async def add_reminders(chat_id: int, text: str):
    # ✅ timezone-aware UTC time
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


async def list_pending_rows(chat_id: int):
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


async def next_pending_row(chat_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow("""
            SELECT text, remind_at
            FROM reminders
            WHERE sent = FALSE AND chat_id = $1
            ORDER BY remind_at
            LIMIT 1
        """, chat_id)
        return row
    finally:
        await conn.close()


async def fetch_due(limit: int = 50):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT id, chat_id, text, remind_at
            FROM reminders
            WHERE sent = FALSE AND remind_at <= NOW()
            ORDER BY remind_at
            LIMIT $1
        """, limit)
        return rows
    finally:
        await conn.close()


async def mark_sent(reminder_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "UPDATE reminders SET sent = TRUE WHERE id = $1",
            reminder_id
        )
    finally:
        await conn.close()


# ---------------- HELPERS ----------------

def human_left(target_dt) -> str:
    """target_dt is timezone-aware from asyncpg (TIMESTAMPTZ)."""
    now = datetime.now(timezone.utc)
    seconds_left = int((target_dt - now).total_seconds())
    if seconds_left <= 0:
        return "hozir"

    days = seconds_left // 86400
    hours = (seconds_left % 86400) // 3600
    mins = (seconds_left % 3600) // 60

    if days > 0:
        return f"{days} kun {hours} soat qoldi"
    if hours > 0:
        return f"{hours} soat {mins} daqiqa qoldi"
    return f"{mins} daqiqa qoldi"


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Men takrorlash botiman ✅\n\n"
        "➕ Qo‘shish:\n"
        "/add Bugun o‘rgangan mavzu\n\n"
        "📋 Ro‘yxat:\n"
        "/list\n\n"
        "⏭ Eng yaqin eslatma:\n"
        "/next\n\n"
        "⏰ Takrorlash: 1 / 3 / 7 / 30"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("✍️ Yozing:\n/add Bugun o‘rgangan mavzu")
        return

    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()

    await add_reminders(chat_id, text)

    await update.message.reply_text(
        f"✅ Saqlandi!\n📌 {text}\n⏰ 1 / 3 / 7 / 30 kunda eslatadi."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await list_pending_rows(chat_id)

    if not rows:
        await update.message.reply_text("📭 Aktiv eslatmalar yo‘q. /add bilan qo‘shing.")
        return

    # Bitta mavzu uchun 4 ta remind bo‘ladi. Chiroyli ko‘rinishi uchun:
    # har bir text bo‘yicha eng yaqin remind_at ni chiqaramiz.
    nearest = defaultdict(lambda: None)
    for r in rows:
        t = r["text"]
        ra = r["remind_at"]
        if nearest[t] is None or ra < nearest[t]:
            nearest[t] = ra

    msg_lines = ["📋 Aktiv mavzular (eng yaqin takrorlash bilan):\n"]
    for t, ra in nearest.items():
        msg_lines.append(f"• {t}\n  ⏳ {human_left(ra)}")

    msg_lines.append("\nℹ️ Har mavzu uchun 1/3/7/30 kunda 4 ta eslatma yuboriladi.")
    await update.message.reply_text("\n".join(msg_lines))


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    row = await next_pending_row(chat_id)

    if not row:
        await update.message.reply_text("📭 Yaqin eslatma yo‘q. /add bilan qo‘shing.")
        return

    await update.message.reply_text(
        "⏭ Eng yaqin takrorlash:\n\n"
        f"📌 {row['text']}\n"
        f"⏳ {human_left(row['remind_at'])}"
    )


# ---------------- WORKER ----------------

async def reminder_worker(app):
    while True:
        try:
            due_rows = await fetch_due(limit=50)
            for r in due_rows:
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
    await init_db()
    # ✅ start worker after app init
    asyncio.create_task(reminder_worker(app))


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables’da BOT_TOKEN qo‘shing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q. Railway Postgres ulanmagan (Variables’da bo‘lishi kerak).")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("next", next_cmd))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
