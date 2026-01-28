import os
import asyncio
import asyncpg
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

REMIND_DAYS = [1, 3, 7, 30]
WORKER_INTERVAL_SEC = 20


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def epoch_to_str(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M (UTC)")


async def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q. Railway Variables’da Postgres.DATABASE_URL ni DATABASE_URL ga bog‘lang.")
    return await asyncpg.connect(DATABASE_URL)


async def ensure_schema():
    conn = await get_conn()
    try:
        # YANGI schema (faqat epoch)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            remind_in_days INT NOT NULL,
            remind_at_epoch BIGINT NOT NULL,
            created_at_epoch BIGINT NOT NULL,
            sent BOOLEAN NOT NULL DEFAULT FALSE,
            sent_at_epoch BIGINT
        );
        """)

        # Indexlar
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat ON reminders(chat_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at_epoch);")
    finally:
        await conn.close()


async def add_reminders(chat_id: int, text: str) -> int:
    now_ep = now_epoch()
    conn = await get_conn()
    try:
        for d in REMIND_DAYS:
            remind_ep = now_ep + d * 24 * 60 * 60
            await conn.execute("""
                INSERT INTO reminders(chat_id, text, remind_in_days, remind_at_epoch, created_at_epoch, sent)
                VALUES ($1, $2, $3, $4, $5, FALSE)
            """, chat_id, text, d, remind_ep, now_ep)
        return len(REMIND_DAYS)
    finally:
        await conn.close()


async def fetch_pending(chat_id: int):
    conn = await get_conn()
    try:
        return await conn.fetch("""
            SELECT id, text, remind_in_days, remind_at_epoch
            FROM reminders
            WHERE chat_id=$1 AND sent=FALSE
            ORDER BY remind_at_epoch ASC
        """, chat_id)
    finally:
        await conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! 👋\n\n"
        "✅ /add <matn> — 1/3/7/30 kunga eslatma saqlaydi\n"
        "📋 /list — saqlangan eslatmalar\n"
        "⏭ /next — eng yaqin eslatma\n\n"
        "Misol:\n"
        "/add Bugun o‘rgangan mavzu: SQL JOIN"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Format: /add <matn>\nMisol: /add Bugun o‘rgangan mavzu")
        return

    text = " ".join(context.args).strip()
    chat_id = update.effective_chat.id

    try:
        await add_reminders(chat_id, text)
        await update.message.reply_text(
            f"✅ Saqlandi!\n📝 {text}\n\n"
            f"⏰ Eslatma: {', '.join(map(str, REMIND_DAYS))} kunda."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ /add xato: {type(e).__name__}")
        raise


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await fetch_pending(chat_id)
    if not rows:
        await update.message.reply_text("📭 Hozircha saqlangan eslatma yo‘q.")
        return

    now_ep = now_epoch()

    grouped = {}
    for r in rows:
        grouped.setdefault(r["text"], []).append(r)

    lines = ["📋 Saqlangan eslatmalar:\n"]
    for text, items in grouped.items():
        lines.append(f"📝 {text}")
        for it in items:
            left_sec = max(0, it["remind_at_epoch"] - now_ep)
            left_days = left_sec // (24 * 3600)
            lines.append(f"  • {it['remind_in_days']} kunlik — ⏳ {left_days} kun qoldi (⏰ {epoch_to_str(it['remind_at_epoch'])})")
        lines.append("")

    await update.message.reply_text("\n".join(lines).strip())


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await fetch_pending(chat_id)
    if not rows:
        await update.message.reply_text("📭 Yaqin eslatma yo‘q.")
        return

    r = rows[0]
    now_ep = now_epoch()
    left_sec = max(0, r["remind_at_epoch"] - now_ep)
    left_days = left_sec // (24 * 3600)

    await update.message.reply_text(
        "⏭ Eng yaqin eslatma:\n\n"
        f"📝 {r['text']}\n"
        f"⏳ {left_days} kun qoldi\n"
        f"⏰ {epoch_to_str(r['remind_at_epoch'])}"
    )


async def reminder_worker(app):
    while True:
        try:
            now_ep = now_epoch()
            conn = await get_conn()
            try:
                due = await conn.fetch("""
                    SELECT id, chat_id, text, remind_in_days
                    FROM reminders
                    WHERE sent=FALSE AND remind_at_epoch <= $1
                    ORDER BY remind_at_epoch ASC
                    LIMIT 50
                """, now_ep)

                for r in due:
                    await app.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"⏰ Eslatma ({r['remind_in_days']} kun):\n📝 {r['text']}"
                    )
                    await conn.execute("""
                        UPDATE reminders
                        SET sent=TRUE, sent_at_epoch=$2
                        WHERE id=$1
                    """, r["id"], now_ep)
            finally:
                await conn.close()
        except Exception:
            pass

        await asyncio.sleep(WORKER_INTERVAL_SEC)


async def post_init(app):
    await ensure_schema()
    app.create_task(reminder_worker(app))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables’da BOT_TOKEN qo‘shing.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("next", next_cmd))

    print("🤖 Bot ishga tushdi...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
