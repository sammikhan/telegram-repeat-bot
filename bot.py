import os
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ====== SETTINGS ======
REMIND_DAYS = [1, 3, 7, 30]  # siz xohlagan: 1/3/7/30
CHECK_EVERY_SECONDS = 15     # worker DB ni tekshiradi
TZ_LOCAL = ZoneInfo("Asia/Tashkent")  # GMT+5

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

pool: asyncpg.Pool | None = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt_utc: datetime) -> datetime:
    # dt_utc timezone-aware bo‘lishi shart (UTC)
    return dt_utc.astimezone(TZ_LOCAL)


async def ensure_schema():
    """Table/columns/indexlar bo‘lmasa yaratadi (avtomatik migrate)."""
    global pool
    assert pool is not None

    async with pool.acquire() as conn:
        # Table
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            days_after INT NOT NULL,
            remind_at TIMESTAMPTZ NOT NULL,
            sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ NULL
        );
        """)

        # Eski versiyadan qolgan bo‘lishi mumkin: columnlar yo‘q bo‘lsa qo‘shib qo‘yamiz
        await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS days_after INT;")
        await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS remind_at TIMESTAMPTZ;")
        await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent BOOLEAN NOT NULL DEFAULT FALSE;")
        await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
        await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;")

        # Indexlar (remind_at_epoch ishlatmaymiz!)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat ON reminders(chat_id, sent, remind_at);")


async def add_reminders(chat_id: int, text: str):
    global pool
    assert pool is not None

    created = 0
    base = now_utc()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for d in REMIND_DAYS:
                remind_at = base + timedelta(days=d)  # UTC aware
                await conn.execute(
                    """
                    INSERT INTO reminders(chat_id, text, days_after, remind_at, sent)
                    VALUES($1, $2, $3, $4, FALSE)
                    """,
                    chat_id, text, d, remind_at
                )
                created += 1
    return created


async def fetch_pending(chat_id: int, limit: int = 50):
    global pool
    assert pool is not None

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, days_after, remind_at, sent
            FROM reminders
            WHERE chat_id = $1 AND sent = FALSE
            ORDER BY remind_at ASC
            LIMIT $2
            """,
            chat_id, limit
        )
    return rows


async def reminder_worker(app):
    """DB dan due bo‘lgan reminderlarni olib, yuboradi va sent=true qiladi.
       FOR UPDATE SKIP LOCKED -> dubl yuborishni kesadi (2 instance bo‘lsa ham).
    """
    global pool
    assert pool is not None

    while True:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Due bo‘lganlarini lock qilib olamiz
                    rows = await conn.fetch(
                        """
                        WITH cte AS (
                            SELECT id, chat_id, text, days_after, remind_at
                            FROM reminders
                            WHERE sent = FALSE AND remind_at <= NOW()
                            ORDER BY remind_at ASC
                            LIMIT 25
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE reminders r
                        SET sent = TRUE, sent_at = NOW()
                        FROM cte
                        WHERE r.id = cte.id
                        RETURNING cte.id, cte.chat_id, cte.text, cte.days_after, cte.remind_at
                        """
                    )

            # Transaction tugadi -> endi yuboramiz
            for r in rows:
                chat_id = int(r["chat_id"])
                text = r["text"]
                d = int(r["days_after"])
                ra = r["remind_at"]  # timestamptz (aware)
                ra_local = to_local(ra)

                msg = (
                    f"⏰ <b>Takrorlash vaqti!</b>\n"
                    f"📌 <b>{d} kun</b> eslatma\n"
                    f"🗓 <b>{ra_local:%d.%m.%Y %H:%M}</b>\n\n"
                    f"📝 {text}"
                )
                await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

        except Exception as e:
            # worker yiqilib qolmasin
            print("Worker error:", repr(e))

        await asyncio.sleep(CHECK_EVERY_SECONDS)


# ====== COMMANDS ======
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot ishga tushdi!\n\n"
        "Buyruqlar:\n"
        "✅ /add <matn>  — 1/3/7/30 kunga eslatma qo‘yadi\n"
        "📋 /list        — saqlangan eslatmalar va qancha qolganini ko‘rsatadi\n"
        "\nMisol:\n"
        "/add Bugun o‘rgangan mavzu: Docker registry"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text("❗ Foydalanish: /add <matn>\nMisol: /add Bugun o‘rgangan mavzu")
            return

        text = " ".join(context.args).strip()
        chat_id = update.effective_chat.id

        n = await add_reminders(chat_id, text)

        await update.message.reply_text(
            f"✅ Saqlandi! ({n} ta eslatma)\n"
            f"📅 {', '.join(map(str, REMIND_DAYS))} kunlarda xabar beraman.\n\n"
            f"📋 Ko‘rish uchun: /list"
        )

    except Exception as e:
        print("add_cmd error:", repr(e))
        await update.message.reply_text(f"❌ /add xato: {type(e).__name__}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        rows = await fetch_pending(chat_id)

        if not rows:
            await update.message.reply_text("📭 Hozircha pending eslatma yo‘q.\nYangi qo‘shish: /add <matn>")
            return

        now = now_utc()
        lines = ["📋 <b>Pending eslatmalar:</b>\n"]
        for r in rows:
            rid = r["id"]
            text = r["text"]
            d = int(r["days_after"])
            ra = r["remind_at"]
            left = ra - now
            # Qolgan vaqtni “kun/soat” ko‘rinishda chiqaramiz
            total_seconds = int(left.total_seconds())
            if total_seconds < 0:
                remain_str = "hozir"
            else:
                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                if days > 0:
                    remain_str = f"{days} kun {hours} soat"
                else:
                    remain_str = f"{hours} soat"

            ra_local = to_local(ra)
            short = text if len(text) <= 60 else text[:57] + "…"
            lines.append(
                f"• <code>#{rid}</code> — <b>{d} kun</b> | "
                f"🗓 {ra_local:%d.%m.%Y %H:%M} | ⏳ {remain_str}\n"
                f"  📝 {short}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as e:
        print("list_cmd error:", repr(e))
        await update.message.reply_text(f"❌ /list xato: {type(e).__name__}")


async def post_init(app):
    global pool
    # Pool yaratamiz
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=60)

    # Schema tayyorlaymiz
    await ensure_schema()

    # Background worker
    app.create_task(reminder_worker(app))

    print("🤖 Bot ishga tushdi...")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables’da BOT_TOKEN qo‘shing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL yo‘q. Railway Variables’da DATABASE_URL qo‘shing.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))

    # IMPORTANT: asyncio.run ishlatmaymiz!
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
