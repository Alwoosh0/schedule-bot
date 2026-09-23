import asyncio
import logging
import os
import re
import sqlite3
import zoneinfo
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from calendar_pdf import DAY_NAMES, MONTH_NAMES, Event, render_day, render_week

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не спамить логами каждого запроса
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # set as env var on Railway
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN не задан. Локально: export BOT_TOKEN=... "
        "(PowerShell: $env:BOT_TOKEN=\"...\"), на Railway: Variables -> BOT_TOKEN"
    )

# Часовые пояса берём из pip-пакета tzdata, а не из системы: в Docker-образах
# системная база бывает устаревшей (Казахстан перешёл на UTC+5 в марте 2024),
# а на Windows её нет вовсе.
zoneinfo.reset_tzpath(to=())
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Almaty"))

# Если к сервису на Railway подключён Volume, Railway сам выставляет
# RAILWAY_VOLUME_MOUNT_PATH — кладём базу туда. Локально — рядом с bot.py.
DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "schedule.db"
)
DEFAULT_REMINDER_MIN = int(os.environ.get("DEFAULT_REMINDER_MIN", "10"))
MAX_REMINDER_MIN = 24 * 60
REMINDER_OFF = -1
MAX_TITLE_LEN = 100

ALL_DAYS = list(range(7))
WEEKDAYS = [0, 1, 2, 3, 4]
WEEKEND = [5, 6]

# Полные названия на английском и русском. Короткие (mon, пн) тоже понимаем,
# чтобы не ломать привычку, но сами их нигде не показываем.
DAY_ALIASES = {
    alias: idx
    for idx, aliases in enumerate([
        ("monday", "mon", "понедельник", "пн"),
        ("tuesday", "tue", "tues", "вторник", "вт"),
        ("wednesday", "wed", "среда", "среду", "ср"),
        ("thursday", "thu", "thur", "thurs", "четверг", "чт"),
        ("friday", "fri", "пятница", "пятницу", "пт"),
        ("saturday", "sat", "суббота", "субботу", "сб"),
        ("sunday", "sun", "воскресенье", "вс"),
    ])
    for alias in aliases
}
DAY_GROUPS = {
    "daily": ALL_DAYS, "everyday": ALL_DAYS, "ежедневно": ALL_DAYS, "все": ALL_DAYS,
    "weekdays": WEEKDAYS, "будни": WEEKDAYS,
    "weekend": WEEKEND, "weekends": WEEKEND, "выходные": WEEKEND,
}
FILLER_WORDS = {"and", "и", "&", "every", "on", "в", "во", "по"}

# 10:00-12:00, 10:00 – 12:00, 9:30-10, 10-12, 8.00 или просто 8:00.
# Одиночное число без минут ("8 Wake up") временем не считаем — слишком двусмысленно.
TIME_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?(?:\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?)?(?![\d:])"
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # commit on success, rollback on error
            yield conn
    finally:
        conn.close()


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                days TEXT NOT NULL,        -- comma separated weekday ints, Mon=0
                start_time TEXT NOT NULL,  -- HH:MM
                end_time TEXT NOT NULL,    -- HH:MM, equals start_time for "8:00 Wake up"
                reminder_min INTEGER NOT NULL DEFAULT {default_min},  -- -1 = off
                num INTEGER                -- number the user sees, per chat, reused after /del
            )
            """.format(default_min=DEFAULT_REMINDER_MIN)
        )

        # Миграция со старой версии: там был только глобальный id, а номер
        # для пользователя должен быть своим в каждом чате и занимать дырки.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "num" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN num INTEGER")
        for r in conn.execute("SELECT id, chat_id FROM events WHERE num IS NULL ORDER BY id").fetchall():
            conn.execute(
                "UPDATE events SET num = ? WHERE id = ?", (next_free_num(conn, r["chat_id"]), r["id"])
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_chat_num ON events (chat_id, num)")


def next_free_num(conn: sqlite3.Connection, chat_id: int) -> int:
    """Smallest number not used in this chat: after deleting #1, the next event gets #1 again."""
    taken = {r[0] for r in conn.execute("SELECT num FROM events WHERE chat_id = ?", (chat_id,))}
    num = 1
    while num in taken:
        num += 1
    return num


def chat_events(chat_id: int, order: str = "start_time, num") -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            f"SELECT * FROM events WHERE chat_id = ? ORDER BY {order}", (chat_id,)
        ).fetchall()


def row_days(row: sqlite3.Row) -> list[int]:
    return [int(d) for d in row["days"].split(",")]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_days(raw: str) -> list[int]:
    """'monday wednesday' / 'понедельник, среда' / 'monday-friday' / 'weekdays' -> [0, 2].

    Пусто -> каждый день.
    """
    raw = raw.lower().replace("ё", "е")
    raw = raw.replace("every day", "daily").replace("каждый день", "daily")
    raw = re.sub(r"\s*[-–—]\s*", "-", raw)
    tokens = [t for t in re.split(r"[,;\s]+", raw) if t and t not in FILLER_WORDS]
    if not tokens:
        return ALL_DAYS

    days = set()
    for token in tokens:
        if token in DAY_GROUPS:
            days.update(DAY_GROUPS[token])
            continue
        if token in DAY_ALIASES:
            days.add(DAY_ALIASES[token])
            continue
        first, _, last = token.partition("-")
        if first in DAY_ALIASES and last in DAY_ALIASES:  # monday-friday
            day = DAY_ALIASES[first]
            while True:
                days.add(day)
                if day == DAY_ALIASES[last]:
                    break
                day = (day + 1) % 7
            continue
        raise ValueError(
            f"I don't know the day “{escape(token)}”.\n"
            "Use monday … sunday (or понедельник … воскресенье), weekdays, weekend or daily."
        )
    return sorted(days)


def parse_clock(hours: str, minutes: str, allow_midnight_end: bool = False) -> str:
    h, m = int(hours), int(minutes)
    if (h, m) == (24, 0) and allow_midnight_end:
        return "24:00"
    if h > 23 or m > 59:
        raise ValueError(f"{hours}:{minutes} is not a valid time.")
    return f"{h:02d}:{m:02d}"


def parse_event(text: str) -> tuple[list[int], str, str, str]:
    """'monday wednesday 18:00-19:30 Gym' -> (days, start, end, title).

    Дни — всё до времени (можно не указывать = каждый день), название — всё после.
    """
    m = next((m for m in TIME_RE.finditer(text) if m[2] or m[3]), None)
    if not m:
        raise ValueError("Add a time: <code>10:00-12:00</code> or just <code>8:00</code>.")
    days = parse_days(text[:m.start()])
    start = parse_clock(m[1], m[2] or "00")
    end = parse_clock(m[3], m[4] or "00", allow_midnight_end=True) if m[3] else start
    title = text[m.end():].strip(" -–—:,")
    if not title:
        raise ValueError("Add a title after the time, e.g. <code>/add 8:00 Wake up</code>.")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"The title is too long — keep it under {MAX_TITLE_LEN} characters.")
    return days, start, end, title


def to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_days(days: list[int]) -> str:
    if days == ALL_DAYS:
        return "Every day"
    if days == WEEKDAYS:
        return "Weekdays"
    if days == WEEKEND:
        return "Weekends"

    # 0,1,2,3 -> "Monday – Thursday"
    parts, run = [], [days[0]]
    for d in days[1:] + [None]:
        if d is not None and d == run[-1] + 1:
            run.append(d)
            continue
        if len(run) >= 3:
            parts.append(f"{DAY_NAMES[run[0]]} – {DAY_NAMES[run[-1]]}")
        else:
            parts.extend(DAY_NAMES[i] for i in run)
        if d is not None:
            run = [d]
    return ", ".join(parts)


def fmt_time(row: sqlite3.Row) -> str:
    if row["start_time"] == row["end_time"]:
        return row["start_time"]
    return f"{row['start_time']}–{row['end_time']}"


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def fmt_reminder(minutes: int) -> str:
    if minutes == REMINDER_OFF:
        return "🔕 no reminder"
    if minutes == 0:
        return "🔔 at start"
    return f"🔔 {fmt_minutes(minutes)} before"


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTH_NAMES[d.month - 1]}"


def agenda_lines(rows: list[sqlite3.Row]) -> list[str]:
    """Время в моноширинном блоке одинаковой ширины, чтобы названия стояли ровным столбиком."""
    return [f"<code>{fmt_time(r):<11}</code>  {escape(r['title'])}" for r in rows]


# ---------------------------------------------------------------------------
# Reminder scheduling
# ---------------------------------------------------------------------------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    when = "starts now" if data["reminder_min"] == 0 else f"in {fmt_minutes(data['reminder_min'])}"
    text = f"⏰ <b>{escape(data['title'])}</b> — {when}\n<code>{data['time']}</code>"
    try:
        await context.bot.send_message(chat_id=data["chat_id"], text=text, parse_mode=ParseMode.HTML)
    except Forbidden:
        logger.warning("Chat %s blocked the bot, reminder skipped", data["chat_id"])


def reminder_schedule(start_hhmm: str, minutes: int, days: list[int]) -> tuple[dtime, tuple[int, ...]]:
    """Время напоминания и дни недели в формате PTB.

    У нас дни: Пн=0 ... Вс=6 (как datetime.weekday()).
    В PTB 20+ run_daily считает дни как cron: Вс=0, Пн=1 ... Сб=6.
    Если напоминание уходит за полночь (занятие в 00:05, напоминание за 10 мин),
    оно должно сработать в предыдущий день.
    """
    day_shift, total = divmod(to_minutes(start_hhmm) - minutes, 24 * 60)  # day_shift <= 0
    remind_at = dtime(hour=total // 60, minute=total % 60, tzinfo=TIMEZONE)
    ptb_days = tuple(sorted({(d + day_shift + 1) % 7 for d in days}))
    return remind_at, ptb_days


def schedule_event_jobs(app: Application, event: sqlite3.Row) -> None:
    unschedule_event_jobs(app, event["id"])
    if event["reminder_min"] == REMINDER_OFF:
        return

    remind_at, ptb_days = reminder_schedule(event["start_time"], event["reminder_min"], row_days(event))
    app.job_queue.run_daily(
        send_reminder,
        time=remind_at,
        days=ptb_days,
        name=f"reminder_{event['id']}",
        data={
            "chat_id": event["chat_id"],
            "title": event["title"],
            "time": fmt_time(event),
            "reminder_min": event["reminder_min"],
        },
    )


def unschedule_event_jobs(app: Application, event_id: int) -> None:
    for job in app.job_queue.get_jobs_by_name(f"reminder_{event_id}"):
        job.schedule_removal()


BOT_COMMANDS = [
    BotCommand("today", "Today's plan"),
    BotCommand("week", "The whole week"),
    BotCommand("add", "Add an event"),
    BotCommand("list", "All events with their numbers"),
    BotCommand("pdf", "PDF calendar: /pdf week or /pdf day"),
    BotCommand("del", "Delete an event"),
    BotCommand("remind", "Change or turn off a reminder"),
    BotCommand("help", "How to use the bot"),
]


async def post_init(app: Application) -> None:
    with db() as conn:
        rows = conn.execute("SELECT * FROM events").fetchall()
    for row in rows:
        schedule_event_jobs(app, row)
    logger.info("Scheduled reminders for %d events", len(rows))
    await app.bot.set_my_commands(BOT_COMMANDS)  # меню команд по кнопке «/» в Telegram


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

ADD_HELP = """
<b>➕ Add an event</b>
<code>/add</code> <i>days</i>  <i>time</i>  <i>title</i>

<code>/add weekdays 10:00-12:00 Java Backend</code>
<code>/add monday wednesday 18:00-19:30 Gym</code>
<code>/add 8:00 Wake up</code>

<b>days</b> — monday … sunday or понедельник … воскресенье, a range like monday-friday, or weekdays · weekend · daily. Skip it = every day.
<b>time</b> — a range <code>10:00-12:00</code> or a single time <code>8:00</code>.
""".strip()

HELP_TEXT = f"""
📅 <b>Your weekly schedule</b>
Add an event once — I'll show it every week and remind you before it starts.

{ADD_HELP}

<b>👀 View</b>
/today — today's plan
/week — the whole week
/list — all events with their numbers

<b>📄 PDF calendar</b>
/pdf week — this week as a calendar
/pdf day — today  ·  also <code>/pdf tomorrow</code>, <code>/pdf friday</code>

<b>⚙️ Manage</b>
<code>/del 3</code> — delete event #3  (several: <code>/del 3 5</code>)
<code>/remind 3 15</code> — remind 15 min before
<code>/remind 3 off</code> — no reminder
""".strip()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = escape(update.effective_user.first_name or "there")
    await update.message.reply_html(f"👋 Hi, {name}!\n\n{HELP_TEXT}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    if not text:
        await update.message.reply_html(ADD_HELP)
        return

    try:
        days, start_time, end_time, title = parse_event(text)
    except ValueError as e:
        await update.message.reply_html(f"⚠️ {e}\n\nSee /help for examples.")
        return

    chat_id = update.effective_chat.id
    with db() as conn:
        num = next_free_num(conn, chat_id)
        cur = conn.execute(
            "INSERT INTO events (chat_id, num, title, days, start_time, end_time, reminder_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, num, title, ",".join(map(str, days)), start_time, end_time, DEFAULT_REMINDER_MIN),
        )
        row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()

    schedule_event_jobs(context.application, row)
    await update.message.reply_html(
        f"✅ <b>{escape(title)}</b> added as <b>#{num}</b>\n"
        f"{fmt_days(days)}  ·  <code>{fmt_time(row)}</code>\n"
        f"{fmt_reminder(row['reminder_min'])}\n\n"
        f"<i>Change the reminder: /remind {num} 15  ·  delete: /del {num}</i>"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = chat_events(update.effective_chat.id, order="num")
    if not rows:
        await update.message.reply_html("Your schedule is empty.\n\n" + ADD_HELP)
        return

    lines = [f"📋 <b>All events</b>  ({len(rows)})", ""]
    for r in rows:
        lines.append(f"<b>#{r['num']}  {escape(r['title'])}</b>")
        lines.append(
            f"{fmt_days(row_days(r))}  ·  <code>{fmt_time(r)}</code>  ·  {fmt_reminder(r['reminder_min'])}"
        )
        lines.append("")
    lines.append("<i>Delete: /del 1  ·  reminder: /remind 1 15</i>")
    await update.message.reply_html("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(TIMEZONE).date()
    weekday = today.weekday()
    todays = [r for r in chat_events(update.effective_chat.id) if weekday in row_days(r)]

    lines = [f"☀️ <b>{DAY_NAMES[weekday]}</b>, {fmt_date(today)}", ""]
    if todays:
        lines += agenda_lines(todays)
    else:
        lines.append("Nothing planned — free day 🎉")
    await update.message.reply_html("\n".join(lines))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = chat_events(update.effective_chat.id)
    if not rows:
        await update.message.reply_html("Your schedule is empty.\n\n" + ADD_HELP)
        return

    today = datetime.now(TIMEZONE).date()
    monday = today - timedelta(days=today.weekday())
    lines = ["🗓 <b>Your week</b>"]
    for day_idx in range(7):
        day = monday + timedelta(days=day_idx)
        mark = "  <i>← today</i>" if day == today else ""
        lines += ["", f"<b>{DAY_NAMES[day_idx]}</b>, {fmt_date(day)}{mark}"]
        day_rows = [r for r in rows if day_idx in row_days(r)]
        lines += agenda_lines(day_rows) if day_rows else ["<i>free</i>"]
    lines += ["", "<i>📄 Printable calendar: /pdf week</i>"]
    await update.message.reply_html("\n".join(lines))


async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = chat_events(update.effective_chat.id)
    if not rows:
        await update.message.reply_html("Your schedule is empty — nothing to export yet.\n\n" + ADD_HELP)
        return

    now = datetime.now(TIMEZONE)
    today = now.date()
    arg = " ".join(context.args).lower().replace("ё", "е")
    events = [
        Event(r["title"], row_days(r), to_minutes(r["start_time"]), to_minutes(r["end_time"]))
        for r in rows
    ]
    brand = context.bot.first_name

    if arg in ("", "week", "неделя"):
        monday = today - timedelta(days=today.weekday())
        render = lambda: render_week(events, monday, today, brand, now)  # noqa: E731
        filename, caption = f"week-{monday:%Y-%m-%d}.pdf", "🗓 Your week"
    else:
        if arg in ("day", "today", "день", "сегодня"):
            day = today
        elif arg in ("tomorrow", "завтра"):
            day = today + timedelta(days=1)
        elif arg in DAY_ALIASES:  # ближайший такой день, включая сегодня
            day = today + timedelta(days=(DAY_ALIASES[arg] - today.weekday()) % 7)
        else:
            await update.message.reply_html(
                "Usage: <code>/pdf week</code> or <code>/pdf day</code>\n"
                "Also: <code>/pdf tomorrow</code>, <code>/pdf friday</code>"
            )
            return
        render = lambda: render_day(events, day, today, brand, now)  # noqa: E731
        filename = f"{DAY_NAMES[day.weekday()].lower()}-{day:%Y-%m-%d}.pdf"
        caption = f"☀️ {DAY_NAMES[day.weekday()]}, {fmt_date(day)}"

    await update.effective_chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    pdf = await asyncio.to_thread(render)
    await update.message.reply_document(document=BytesIO(pdf), filename=filename, caption=caption)


def parse_nums(args: list[str]) -> list[int] | None:
    nums = re.findall(r"\d+", " ".join(args))
    if not nums or re.sub(r"[\d#,\s]", "", " ".join(args)):
        return None
    return sorted({int(n) for n in nums})


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nums = parse_nums(context.args)
    if not nums:
        await update.message.reply_html(
            "Usage: <code>/del 3</code> or <code>/del 3 5</code>\nEvent numbers are in /list"
        )
        return

    chat_id = update.effective_chat.id
    deleted, missing = [], []
    with db() as conn:
        for num in nums:
            row = conn.execute(
                "SELECT * FROM events WHERE chat_id = ? AND num = ?", (chat_id, num)
            ).fetchone()
            if not row:
                missing.append(num)
                continue
            conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
            deleted.append(row)

    for row in deleted:
        unschedule_event_jobs(context.application, row["id"])

    lines = [f"🗑 Deleted <b>#{r['num']}  {escape(r['title'])}</b>" for r in deleted]
    if missing:
        lines.append("Not found: " + ", ".join(f"#{n}" for n in missing) + "  (see /list)")
    await update.message.reply_html("\n".join(lines))


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    usage = (
        "Usage: <code>/remind 3 15</code> — 15 min before event #3\n"
        "<code>/remind 3 0</code> — at start  ·  <code>/remind 3 off</code> — no reminder"
    )
    if len(args) != 2 or not args[0].lstrip("#").isdigit():
        await update.message.reply_html(usage)
        return

    num = int(args[0].lstrip("#"))
    if args[1].lower() in ("off", "no", "none", "выкл", "нет"):
        minutes = REMINDER_OFF
    elif args[1].isdigit():
        minutes = int(args[1])
        if minutes > MAX_REMINDER_MIN:
            await update.message.reply_text(f"Maximum is {MAX_REMINDER_MIN} minutes (one day).")
            return
    else:
        await update.message.reply_html(usage)
        return

    chat_id = update.effective_chat.id
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE chat_id = ? AND num = ?", (chat_id, num)
        ).fetchone()
        if not row:
            await update.message.reply_html(f"No event #{num}. See /list")
            return
        conn.execute("UPDATE events SET reminder_min = ? WHERE id = ?", (minutes, row["id"]))
        row = conn.execute("SELECT * FROM events WHERE id = ?", (row["id"],)).fetchone()

    schedule_event_jobs(context.application, row)
    await update.message.reply_html(
        f"<b>#{num}  {escape(row['title'])}</b>\n{fmt_reminder(minutes)}"
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html("I don't know this command. See /help")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "To add an event, start with /add, e.g.\n<code>/add monday 18:00-19:30 Gym</code>\n\nAll commands: /help"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()

    if os.environ.get("RAILWAY_PROJECT_ID") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        logger.warning(
            "Volume не подключён — база %s будет стираться при каждом редеплое!", DB_PATH
        )

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("pdf", pdf_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, plain_text))

    logger.info(
        "Bot starting (timezone=%s, local time %s, db=%s)...",
        TIMEZONE,
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        DB_PATH,
    )
    # Только новые сообщения: иначе правка старого «/add ...» добавила бы событие ещё раз
    app.run_polling(allowed_updates=[Update.MESSAGE])


if __name__ == "__main__":
    main()
