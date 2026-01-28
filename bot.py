import os
import asyncio
import asyncpg
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

REMIND_DAYS = [1, 3, 7, 30]


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def epoch_to_dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def human_left_epoch(target_epoch: int) -> str:
    left = target_epoch - now_epoch()
    if left <= 0:
        return "hozir"
    days = left // 86400
    hours = (left % 86400) // 3600
    mins = (left % 3600) // 60
    if days > 0:
        return f"{days} kun {hours} soat qoldi"
    if hours > 0:
        return f"{hours} soat {mins} daqiqa qoldi"
    return f"{mins} daqiqa qoldi"


# -------- DB --------
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # epoch_seconds = BIGINT -> timezone muammosi yo'q
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                remind_at_epoch BIGINT NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT FALSE
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat_sent_time ON reminders(chat_id, sent, remind_at_epoch);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_sent_time ON reminders(sent, remind_at_epoch);")
    finally:
        await conn.close()


async def add_reminders(chat_id: int, text: str):
    base = now_epoch()
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        for d in REMIND_DAYS:
            remind_epoch = base + d * 86400
            await conn.execute(
                "INSERT INTO reminders(chat_id, text, remind_at_epoch) VALUES ($1, $2, $3)",
                chat_id, text, remind_epoch
            )
    finally:
        await conn.close()


async def list_pending_rows(chat_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetch("""
            SELECT text, remind_at_epoch
            FROM reminders
            WHERE sent = FALSE AND chat_id = $1
            ORDER BY remind_at_epoch
        """, chat_id)
    finally:
        await conn.close()


async def next_pending_row(chat_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetchrow("""
            SELECT text, remind_at_epoch
            FROM reminders
            WHERE sent = FALSE AND chat_id = $1
            ORDER BY remind_at_epoch
            LIMIT 1
        """, chat_id)
    finally:
        await conn.close()


async def fetch_due(limit: int = 50):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetch("""
            SELECT id, chat_id, text
            FROM reminders
            WHERE sent = FALSE AND remind_at_epoch <= $1
            ORDER BY remind_at_epoch
            LIMIT $2
        """, now_epoch(), limit)
    finally:
        await conn.close()


async def mark_sent(reminder_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("UPDATE reminders SET sent = TRUE WHERE id = $1", reminder_id)
    finally:
        await conn.close()


# -------- commands --------
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

    try:
        await add_reminders(chat_id, text)
        await update.message.reply_text(
            f"✅ Saqlandi!\n📌 {text}\n⏰ 1 / 3 / 7 / 30 kunda eslatadi."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ /add xato: {type(e).__name__}")
        raise


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await list_pending_rows(chat_id)

    if not rows:
        await update.message.reply_text("📭 Aktiv eslatmalar yo‘q. /add bilan qo‘shing.")
        return

    nearest = defaultdict(lambda: None)
    for r in rows:
        t = r["text"]
        ra = int(r["remind_at_epoch"])
        if nearest[t] is None or ra < nearest[t]:
            nearest[t] = ra

    msg_lines = ["📋 Aktiv mavzular (eng yaqin takrorlash bilan):\n"]
    for t, ra in nearest.items():
        msg_lines.append(f"• {t}\n  ⏳ {human_left_epoch(ra)}")

    msg_lines.append("\nℹ️ Har mavzu uchun 1/3/7/30 kunda 4 ta eslatma yuboriladi.")
    await update.message.reply_text("\n".join(msg_lines))


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    row = await next_pending_row(chat_id)

    if not row:
        await update.message.reply_text("📭 Yaqin eslatma yo‘q. /add bilan qo‘shing.")
        return

    ra = int(row["remind_at_epoch"])
    await update.message.reply_text(
        "⏭ Eng yaqin takrorlash:\n\n"
        f"📌 {row['text']}\n"
        f"⏳ {human_left_epoch(ra)}"
    )


# -------- worker --------
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
    asyncio.create_task(reminder_worker(app))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables’da BOT_TOKEN qo‘shing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q. Railway Postgres ulanmagan.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("next", next_cmd))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
