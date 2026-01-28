import os
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ====== ENV ======
TOKEN = os.getenv("BOT_TOKEN")
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET", "5"))  # Uzbekistan +5
DB_PATH = "bot.db"

REPEAT_DAYS = [1, 3, 7, 30]


# ====== TIME HELPERS ======
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_local(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=TZ_OFFSET_HOURS)

def fmt_local(dt_utc: datetime) -> str:
    # e.g. 2026-01-28 09:30
    return to_local(dt_utc).strftime("%Y-%m-%d %H:%M")


# ====== DB ======
def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def db_init(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            days INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(item_id) REFERENCES items(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_schedules_chat_due ON schedules(chat_id, due_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_schedules_sent_due ON schedules(sent, due_at)")
    con.commit()

def db_add_item(con: sqlite3.Connection, chat_id: int, text: str, created_at: datetime) -> int:
    cur = con.cursor()
    cur.execute(
        "INSERT INTO items (chat_id, text, created_at) VALUES (?, ?, ?)",
        (chat_id, text, created_at.isoformat())
    )
    con.commit()
    return cur.lastrowid

def db_add_schedule(con: sqlite3.Connection, item_id: int, chat_id: int, due_at: datetime, days: int) -> int:
    cur = con.cursor()
    cur.execute(
        "INSERT INTO schedules (item_id, chat_id, due_at, days, sent) VALUES (?, ?, ?, ?, 0)",
        (item_id, chat_id, due_at.isoformat(), days)
    )
    con.commit()
    return cur.lastrowid

def db_mark_sent(con: sqlite3.Connection, schedule_id: int) -> None:
    con.execute("UPDATE schedules SET sent=1 WHERE id=?", (schedule_id,))
    con.commit()

def db_list_items(con: sqlite3.Connection, chat_id: int, limit: int = 20):
    # For each item, find the next unsent due date
    cur = con.cursor()
    cur.execute("""
        SELECT
            i.id as item_id,
            i.text as text,
            i.created_at as created_at,
            (
              SELECT s.due_at
              FROM schedules s
              WHERE s.item_id = i.id AND s.sent = 0
              ORDER BY s.due_at ASC
              LIMIT 1
            ) as next_due_at,
            (
              SELECT s.days
              FROM schedules s
              WHERE s.item_id = i.id AND s.sent = 0
              ORDER BY s.due_at ASC
              LIMIT 1
            ) as next_days
        FROM items i
        WHERE i.chat_id = ?
        ORDER BY i.id DESC
        LIMIT ?
    """, (chat_id, limit))
    return cur.fetchall()

def db_next_schedules(con: sqlite3.Connection, chat_id: int, limit: int = 10):
    cur = con.cursor()
    cur.execute("""
        SELECT s.id as schedule_id, s.due_at, s.days, i.text
        FROM schedules s
        JOIN items i ON i.id = s.item_id
        WHERE s.chat_id = ? AND s.sent = 0
        ORDER BY s.due_at ASC
        LIMIT ?
    """, (chat_id, limit))
    return cur.fetchall()

def db_pending_future(con: sqlite3.Connection):
    # All future unsent schedules (for restart recovery)
    cur = con.cursor()
    cur.execute("""
        SELECT s.id as schedule_id, s.chat_id, s.due_at, s.days, i.text
        FROM schedules s
        JOIN items i ON i.id = s.item_id
        WHERE s.sent = 0
    """)
    return cur.fetchall()


# ====== JOBS ======
def schedule_one_job(app, schedule_id: int, chat_id: int, due_at_utc: datetime, days: int, text: str):
    # Avoid duplicates by giving each job unique name
    name = f"sch_{schedule_id}"
    # If due time already passed, we can send immediately (optional)
    when = due_at_utc
    app.job_queue.run_once(
        callback=remind_job,
        when=when,
        chat_id=chat_id,
        name=name,
        data={
            "schedule_id": schedule_id,
            "text": text,
            "days": days,
            "due_at": due_at_utc.isoformat()
        }
    )

async def remind_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    schedule_id = int(data["schedule_id"])
    text = data["text"]
    days = int(data["days"])
    due_at = datetime.fromisoformat(data["due_at"])

    # mark sent in DB
    con = db_connect()
    try:
        db_mark_sent(con, schedule_id)
    finally:
        con.close()

    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            f"🔁 Takrorlash vaqti keldi! ({days}-kun)\n"
            f"📘 {text}\n"
            f"🕒 {fmt_local(due_at)} (GMT+{TZ_OFFSET_HOURS})"
        )
    )


# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Men takrorlash botiman ✅\n\n"
        "📌 Mavzu qo‘shish:\n"
        "/add Bugun o‘rgangan mavzu\n\n"
        "📋 Ro‘yxat:\n"
        "/list\n\n"
        "⏭ Eng yaqin takrorlashlar:\n"
        "/next\n\n"
        "⏰ Takrorlash kunlari: 1 / 3 / 7 / 30"
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Iltimos yozing:\n/add Bugun o‘rgangan mavzu")
        return

    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()
    created = now_utc()

    con = db_connect()
    try:
        item_id = db_add_item(con, chat_id, text, created)

        # Create schedules and queue jobs
        for d in REPEAT_DAYS:
            due = created + timedelta(days=d)
            schedule_id = db_add_schedule(con, item_id, chat_id, due, d)
            schedule_one_job(context.application, schedule_id, chat_id, due, d, text)
    finally:
        con.close()

    await update.message.reply_text(
        f"✅ Saqlandi!\n📌 {text}\n⏰ 1, 3, 7, 30 kunda eslatadi."
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    con = db_connect()
    try:
        rows = db_list_items(con, chat_id, limit=20)
    finally:
        con.close()

    if not rows:
        await update.message.reply_text("Hozircha hech narsa yo‘q. /add bilan qo‘shing.")
        return

    now = now_utc()

    lines = ["📋 Oxirgi mavzular (keyingi takrorlash bilan):"]
    for r in rows:
        text = r["text"]
        next_due_at = r["next_due_at"]
        next_days = r["next_days"]

        if next_due_at is None:
            lines.append(f"✅ {text} — barcha takrorlashlar tugagan")
            continue

        due = datetime.fromisoformat(next_due_at)
        delta = due - now
        # remaining
        total_minutes = int(delta.total_seconds() // 60)
        if total_minutes < 0:
            remain = "hozir"
        else:
            days_left = total_minutes // (60 * 24)
            hours_left = (total_minutes % (60 * 24)) // 60
            if days_left > 0:
                remain = f"{days_left} kun {hours_left} soat qoldi"
            else:
                remain = f"{hours_left} soat qoldi"

        lines.append(f"• {text}\n  ⏭ {next_days}-kun: {fmt_local(due)} — {remain}")

    await update.message.reply_text("\n".join(lines))

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    con = db_connect()
    try:
        rows = db_next_schedules(con, chat_id, limit=10)
    finally:
        con.close()

    if not rows:
        await update.message.reply_text("Yaqin takrorlash yo‘q. /add bilan qo‘shing.")
        return

    now = now_utc()
    lines = ["⏭ Eng yaqin takrorlashlar:"]
    for r in rows:
        due = datetime.fromisoformat(r["due_at"])
        delta = due - now
        mins = int(delta.total_seconds() // 60)
        if mins < 0:
            remain = "hozir"
        else:
            d = mins // (60 * 24)
            h = (mins % (60 * 24)) // 60
            remain = f"{d} kun {h} soat qoldi" if d > 0 else f"{h} soat qoldi"

        lines.append(f"• ({r['days']}-kun) {r['text']}\n  🕒 {fmt_local(due)} — {remain}")

    await update.message.reply_text("\n".join(lines))


# ====== MAIN ======
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables’da BOT_TOKEN qo‘shing.")

    con = db_connect()
    try:
        db_init(con)
    finally:
        con.close()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("next", next_cmd))

    # On startup: re-schedule all pending reminders (after restart/redeploy)
    con = db_connect()
    try:
        pending = db_pending_future(con)
    finally:
        con.close()

    now = now_utc()
    for r in pending:
        due = datetime.fromisoformat(r["due_at"])
        if due <= now:
            # if it already passed, schedule immediately (or skip)
            due = now + timedelta(seconds=5)
        schedule_one_job(
            app,
            schedule_id=int(r["schedule_id"]),
            chat_id=int(r["chat_id"]),
            due_at_utc=due,
            days=int(r["days"]),
            text=r["text"]
        )

    print("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
