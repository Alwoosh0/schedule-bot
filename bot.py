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
PLAN_DAYS = 7  # на сколько дней вперёд шаблон превращается в конкретные дела

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
RELATIVE_DAYS = {"today": 0, "сегодня": 0, "tomorrow": 1, "завтра": 1}
FILLER_WORDS = {"and", "и", "&", "every", "on", "в", "во", "по"}

# 10:00-12:00, 10:00 – 12:00, 9:30-10, 10-12, 8.00 или просто 8:00.
# Одиночное число без минут ("8 Wake up") временем не считаем — слишком двусмысленно.
TIME_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?(?:\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?)?(?![\d:])"
)


# ---------------------------------------------------------------------------
# Database
#
# templates — то, что повторяется каждую неделю (дни недели + время).
# events    — конкретные дела с датой на ближайшие PLAN_DAYS дней: копии из
#             шаблона плюс разовые из /add. Пропущенное через /del занятие из
#             шаблона не удаляется, а помечается cancelled=1, чтобы оно не
#             создалось заново до следующей недели.
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


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with db() as conn:
        # Миграция: в старых версиях events хранили дни недели, то есть были
        # повторяющимися — ровно то, что теперь называется шаблоном.
        if "days" in columns(conn, "events"):
            conn.execute("DROP INDEX IF EXISTS events_chat_num")
            conn.execute("ALTER TABLE events RENAME TO templates")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                days TEXT NOT NULL,        -- comma separated weekday ints, Mon=0
                start_time TEXT NOT NULL,  -- HH:MM
                end_time TEXT NOT NULL,    -- HH:MM, equals start_time for "8:00 Wake up"
                reminder_min INTEGER NOT NULL DEFAULT {default_min},  -- -1 = off
                num INTEGER                -- T-number the user sees, per chat
            )
            """.format(default_min=DEFAULT_REMINDER_MIN)
        )
        if "num" not in columns(conn, "templates"):  # самая первая версия, без номеров
            conn.execute("ALTER TABLE templates ADD COLUMN num INTEGER")
        for r in conn.execute("SELECT id, chat_id FROM templates WHERE num IS NULL ORDER BY id").fetchall():
            conn.execute(
                "UPDATE templates SET num = ? WHERE id = ?",
                (next_free_num(conn, "templates", r["chat_id"]), r["id"]),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS templates_chat_num ON templates (chat_id, num)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                num INTEGER,               -- #number the user sees; NULL once cancelled
                template_id INTEGER,       -- NULL for one-off events from /add
                date TEXT NOT NULL,        -- YYYY-MM-DD
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                reminder_min INTEGER NOT NULL,
                cancelled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_chat_num ON events (chat_id, num)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_template_date ON events (template_id, date)")


def next_free_num(conn: sqlite3.Connection, table: str, chat_id: int) -> int:
    """Smallest number not used in this chat: after deleting #1, the next one gets #1 again."""
    taken = {
        r[0] for r in conn.execute(
            f"SELECT num FROM {table} WHERE chat_id = ? AND num IS NOT NULL", (chat_id,)
        )
    }
    num = 1
    while num in taken:
        num += 1
    return num


def plan_rows(chat_id: int, first: date, last: date | None = None) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE chat_id = ? AND cancelled = 0 AND date BETWEEN ? AND ? "
            "ORDER BY date, start_time, num",
            (chat_id, first.isoformat(), (last or first).isoformat()),
        ).fetchall()


def template_rows(chat_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM templates WHERE chat_id = ? ORDER BY num", (chat_id,)
        ).fetchall()


def fetch_events(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    with db() as conn:
        return conn.execute(
            f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()


def fill_plan(conn: sqlite3.Connection, today: date, templates: list[sqlite3.Row]) -> list[int]:
    """Create dated events from templates for the next PLAN_DAYS days; returns new event ids.

    Уже существующие (в том числе пропущенные через /del) повторно не создаются.
    """
    todo = []
    for t in templates:
        days = row_days(t)
        for i in range(PLAN_DAYS):
            day = today + timedelta(days=i)
            if day.weekday() in days:
                todo.append((day.isoformat(), t["start_time"], t["num"], t))
    todo.sort(key=lambda item: item[:3])  # номера раздаём в хронологическом порядке

    new_ids = []
    for day, _, _, t in todo:
        if conn.execute(
            "SELECT 1 FROM events WHERE template_id = ? AND date = ?", (t["id"], day)
        ).fetchone():
            continue
        cur = conn.execute(
            "INSERT INTO events (chat_id, num, template_id, date, title, start_time, end_time, reminder_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t["chat_id"], next_free_num(conn, "events", t["chat_id"]), t["id"], day,
             t["title"], t["start_time"], t["end_time"], t["reminder_min"]),
        )
        new_ids.append(cur.lastrowid)
    return new_ids


def refresh_plans() -> list[int]:
    """Drop past days and plan the upcoming ones from templates (startup + every midnight)."""
    today = datetime.now(TIMEZONE).date()
    with db() as conn:
        conn.execute("DELETE FROM events WHERE date < ?", (today.isoformat(),))
        return fill_plan(conn, today, conn.execute("SELECT * FROM templates").fetchall())


def row_days(row: sqlite3.Row) -> list[int]:
    return [int(d) for d in row["days"].split(",")]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_days(raw: str, today: date | None = None) -> list[int]:
    """'monday wednesday' / 'понедельник, среда' / 'monday-friday' / 'weekdays' -> [0, 2].

    Для шаблона пусто = каждый день. Для разового дела (передан today) пусто =
    сегодня, и ещё понимаем today / tomorrow.
    """
    raw = raw.lower().replace("ё", "е")
    raw = raw.replace("every day", "daily").replace("каждый день", "daily")
    raw = re.sub(r"\s*[-–—]\s*", "-", raw)
    tokens = [t for t in re.split(r"[,;\s]+", raw) if t and t not in FILLER_WORDS]
    if not tokens:
        return [today.weekday()] if today else ALL_DAYS

    days = set()
    for token in tokens:
        if today and token in RELATIVE_DAYS:
            days.add((today + timedelta(days=RELATIVE_DAYS[token])).weekday())
            continue
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
        extra = "today, tomorrow, " if today else ""
        raise ValueError(
            f"I don't know the day “{escape(token)}”.\n"
            f"Use {extra}monday … sunday (or понедельник … воскресенье), weekdays, weekend or daily."
        )
    return sorted(days)


def parse_clock(hours: str, minutes: str, allow_midnight_end: bool = False) -> str:
    h, m = int(hours), int(minutes)
    if (h, m) == (24, 0) and allow_midnight_end:
        return "24:00"
    if h > 23 or m > 59:
        raise ValueError(f"{hours}:{minutes} is not a valid time.")
    return f"{h:02d}:{m:02d}"


def parse_event(text: str, today: date | None = None) -> tuple[list[int], str, str, str]:
    """'monday wednesday 18:00-19:30 Gym' -> (days, start, end, title).

    Дни — всё до времени, название — всё после.
    """
    m = next((m for m in TIME_RE.finditer(text) if m[2] or m[3]), None)
    if not m:
        raise ValueError("Add a time: <code>10:00-12:00</code> or just <code>8:00</code>.")
    days = parse_days(text[:m.start()], today)
    start = parse_clock(m[1], m[2] or "00")
    end = parse_clock(m[3], m[4] or "00", allow_midnight_end=True) if m[3] else start
    title = text[m.end():].strip(" -–—:,")
    if not title:
        raise ValueError("Add a title after the time, e.g. <code>8:00 Wake up</code>.")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"The title is too long — keep it under {MAX_TITLE_LEN} characters.")
    return days, start, end, title


def parse_nums(args: list[str], prefix: str = "#") -> list[int] | None:
    """'3' / '#3' / '3 5' / '3,5' (for templates also 'T3') -> [3, 5]; None if it's not just numbers."""
    text = " ".join(args).lower().replace(prefix.lower(), "")
    nums = re.findall(r"\d+", text)
    if not nums or re.sub(r"[\d#,\s]", "", text):
        return None
    return sorted({int(n) for n in nums})


def parse_reminder(raw: str) -> int | None:
    if raw.lower() in ("off", "no", "none", "выкл", "нет"):
        return REMINDER_OFF
    if raw.isdigit() and int(raw) <= MAX_REMINDER_MIN:
        return int(raw)
    return None


def to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def plan_date(today: date, weekday: int) -> date:
    """Nearest date with this weekday within the plan, today included."""
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def span(row: sqlite3.Row) -> tuple[int, int]:
    """Minutes interval; a single-time event counts as one minute, overnight ones end at midnight."""
    start, end = to_minutes(row["start_time"]), to_minutes(row["end_time"])
    if end == start:
        return start, start + 1
    return start, end if end > start else 24 * 60


def overlaps(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    (a0, a1), (b0, b1) = span(a), span(b)
    return a0 < b1 and b0 < a1


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


def fmt_day(d: date) -> str:
    return f"{DAY_NAMES[d.weekday()]}, {fmt_date(d)}"


def day_heading(d: date, today: date) -> str:
    mark = {0: "  <i>← today</i>", 1: "  <i>← tomorrow</i>"}.get((d - today).days, "")
    return f"<b>{DAY_NAMES[d.weekday()]}</b>, {fmt_date(d)}{mark}"


def agenda_lines(rows: list[sqlite3.Row]) -> list[str]:
    """Номер и время в моноширинном блоке одинаковой ширины — названия стоят ровным столбиком."""
    return [
        f"<code>{'#' + str(r['num']):<4}{fmt_time(r):<11}</code>  {escape(r['title'])}"
        for r in rows
    ]


def template_lines(t: sqlite3.Row) -> list[str]:
    return [
        f"<b>T{t['num']}  {escape(t['title'])}</b>",
        f"{fmt_days(row_days(t))}  ·  <code>{fmt_time(t)}</code>  ·  {fmt_reminder(t['reminder_min'])}",
    ]


# ---------------------------------------------------------------------------
# Reminders: one run_once job per dated event
# ---------------------------------------------------------------------------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = fetch_events([context.job.data])
    if not rows or rows[0]["cancelled"]:
        return
    row = rows[0]
    when = "starts now" if row["reminder_min"] == 0 else f"in {fmt_minutes(row['reminder_min'])}"
    text = f"⏰ <b>{escape(row['title'])}</b> — {when}\n<code>{fmt_time(row)}</code>"
    try:
        await context.bot.send_message(chat_id=row["chat_id"], text=text, parse_mode=ParseMode.HTML)
    except Forbidden:
        logger.warning("Chat %s blocked the bot, reminder skipped", row["chat_id"])


def schedule_reminder(app: Application, row: sqlite3.Row) -> None:
    unschedule_reminders(app, [row["id"]])
    if row["cancelled"] or row["reminder_min"] == REMINDER_OFF:
        return
    starts = datetime.combine(
        date.fromisoformat(row["date"]), dtime.fromisoformat(row["start_time"]), tzinfo=TIMEZONE
    )
    remind_at = starts - timedelta(minutes=row["reminder_min"])
    if remind_at > datetime.now(TIMEZONE):
        app.job_queue.run_once(send_reminder, when=remind_at, name=f"event_{row['id']}", data=row["id"])


def schedule_events(app: Application, ids: list[int]) -> None:
    for row in fetch_events(ids):
        schedule_reminder(app, row)


def unschedule_reminders(app: Application, ids: list[int]) -> None:
    for event_id in ids:
        for job in app.job_queue.get_jobs_by_name(f"event_{event_id}"):
            job.schedule_removal()


async def midnight_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    new_ids = refresh_plans()
    schedule_events(context.application, new_ids)
    logger.info("New day planned: %d events created from templates", len(new_ids))


BOT_COMMANDS = [
    BotCommand("today", "Today's plan"),
    BotCommand("week", "The next 7 days"),
    BotCommand("add", "Add a one-off plan"),
    BotCommand("template", "Your weekly template"),
    BotCommand("pdf", "PDF calendar: /pdf week or /pdf day"),
    BotCommand("del", "Remove a plan (the template keeps it)"),
    BotCommand("remind", "Change or turn off a reminder"),
    BotCommand("help", "How to use the bot"),
]


async def post_init(app: Application) -> None:
    refresh_plans()
    with db() as conn:
        rows = conn.execute("SELECT * FROM events WHERE cancelled = 0").fetchall()
    for row in rows:
        schedule_reminder(app, row)
    app.job_queue.run_daily(midnight_job, time=dtime(0, 0, 5, tzinfo=TIMEZONE), name="midnight")
    logger.info("Planned %d upcoming events", len(rows))
    await app.bot.set_my_commands(BOT_COMMANDS)  # меню команд по кнопке «/» в Telegram


# ---------------------------------------------------------------------------
# Help texts
# ---------------------------------------------------------------------------

FORMAT_HELP = """
<b>days</b> — monday … sunday or понедельник … воскресенье, monday-friday, weekdays · weekend · daily
<b>time</b> — <code>10:00-12:00</code> or just <code>8:00</code>
""".strip()

TEMPLATE_HELP = f"""
<b>🔁 Weekly template</b> — things that repeat every week
<code>/template add weekdays 9:00-14:00 School</code>
<code>/template add monday wednesday 18:00 Gym</code>
<code>/template edit T2 friday 18:00 Gym</code>
<code>/template del T2</code>  ·  <code>/template remind T2 30</code>
/template — show it
I plan it for the next 7 days automatically. Days skipped = every day.

{FORMAT_HELP}
""".strip()

ADD_HELP = f"""
<b>➕ One-off plans</b> — only once, within the next 7 days
<code>/add friday 15:00 Dentist</code>
<code>/add tomorrow 19:00-21:00 Cinema</code>
<code>/add 18:00 Call mom</code>  ← today

{FORMAT_HELP}
""".strip()

HELP_TEXT = """
📅 <b>Your schedule</b>
Put what repeats every week into the <b>template</b> — I'll turn it into a plan for the next 7 days and remind you before each thing starts.

<b>🔁 Weekly template</b>
<code>/template add monday wednesday 10:00-11:30 Math</code>
<code>/template edit T1 monday 10:00-12:00 Math</code>
<code>/template del T1</code>  ·  /template — show it

<b>➕ One-off plans</b>
<code>/add friday 15:00 Dentist</code>
<code>/add tomorrow 19:00-21:00 Cinema</code>

<b>👀 View</b>
/today — today's plan
/week — the next 7 days
/pdf week  ·  /pdf day  ·  <code>/pdf friday</code>

<b>⚙️ Change the plan</b>
<code>/del 3</code> — skip #3 this time, the template keeps it
<code>/remind 3 15</code>  ·  <code>/remind 3 off</code>

<b>days</b> — monday … sunday or понедельник … воскресенье, monday-friday, weekdays · weekend · daily
<b>time</b> — <code>10:00-12:00</code> or just <code>8:00</code>
""".strip()


# ---------------------------------------------------------------------------
# Plan commands (dated events, next 7 days)
# ---------------------------------------------------------------------------

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

    today = datetime.now(TIMEZONE).date()
    try:
        days, start_time, end_time, title = parse_event(text, today)
    except ValueError as e:
        await update.message.reply_html(f"⚠️ {e}\n\nSee /help for examples.")
        return

    chat_id = update.effective_chat.id
    added, warnings = [], []
    with db() as conn:
        for day in sorted(plan_date(today, wd) for wd in days):
            cur = conn.execute(
                "INSERT INTO events (chat_id, num, date, title, start_time, end_time, reminder_min) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, next_free_num(conn, "events", chat_id), day.isoformat(),
                 title, start_time, end_time, DEFAULT_REMINDER_MIN),
            )
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
            added.append(row)
            same_day = conn.execute(
                "SELECT * FROM events WHERE chat_id = ? AND date = ? AND cancelled = 0 AND id != ?",
                (chat_id, row["date"], row["id"]),
            ).fetchall()
            warnings += [
                f"⚠️ Overlaps with <b>#{o['num']} {escape(o['title'])}</b> on {DAY_NAMES[day.weekday()]}"
                for o in same_day if overlaps(row, o)
            ]

    schedule_events(context.application, [r["id"] for r in added])
    lines = [f"✅ <b>{escape(title)}</b>  ·  <code>{fmt_time(added[0])}</code>"]
    lines += [f"<b>#{r['num']}</b>  {fmt_day(date.fromisoformat(r['date']))}" for r in added]
    lines.append(fmt_reminder(DEFAULT_REMINDER_MIN))
    if warnings:
        lines += [""] + warnings
    lines += ["", "<i>Every week? Put it in the template: /template add …</i>"]
    await update.message.reply_html("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(TIMEZONE).date()
    rows = plan_rows(update.effective_chat.id, today)
    lines = [f"☀️ <b>{DAY_NAMES[today.weekday()]}</b>, {fmt_date(today)}", ""]
    lines += agenda_lines(rows) if rows else ["Nothing planned — free day 🎉"]
    await update.message.reply_html("\n".join(lines))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    today = datetime.now(TIMEZONE).date()
    rows = plan_rows(chat_id, today, today + timedelta(days=PLAN_DAYS - 1))
    if not rows and not template_rows(chat_id):
        await update.message.reply_html(f"Your schedule is empty.\n\n{TEMPLATE_HELP}")
        return

    lines = ["🗓 <b>Next 7 days</b>"]
    for i in range(PLAN_DAYS):
        day = today + timedelta(days=i)
        day_rows = [r for r in rows if r["date"] == day.isoformat()]
        lines += ["", day_heading(day, today)]
        lines += agenda_lines(day_rows) if day_rows else ["<i>free</i>"]
    lines += ["", "<i>Skip one: /del 3  ·  printable: /pdf week  ·  every week: /template</i>"]
    await update.message.reply_html("\n".join(lines))


async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TIMEZONE)
    today = now.date()
    last = today + timedelta(days=PLAN_DAYS - 1)
    rows = plan_rows(update.effective_chat.id, today, last)
    if not rows:
        await update.message.reply_html(
            f"Nothing planned for the next 7 days — nothing to export yet.\n\n{TEMPLATE_HELP}"
        )
        return

    arg = " ".join(context.args).lower().replace("ё", "е")
    events = [
        Event(r["title"], date.fromisoformat(r["date"]), to_minutes(r["start_time"]), to_minutes(r["end_time"]))
        for r in rows
    ]
    brand = context.bot.first_name

    if arg in ("", "week", "неделя"):
        render = lambda: render_week(events, today, today, brand, now)  # noqa: E731
        filename, caption = f"week-{today:%Y-%m-%d}.pdf", f"🗓 {fmt_date(today)} – {fmt_date(last)}"
    else:
        if arg in ("day", "день"):
            day = today
        elif arg in RELATIVE_DAYS:
            day = today + timedelta(days=RELATIVE_DAYS[arg])
        elif arg in DAY_ALIASES:
            day = plan_date(today, DAY_ALIASES[arg])
        else:
            await update.message.reply_html(
                "Usage: <code>/pdf week</code> or <code>/pdf day</code>\n"
                "Also: <code>/pdf tomorrow</code>, <code>/pdf friday</code>"
            )
            return
        render = lambda: render_day(events, day, today, brand, now)  # noqa: E731
        filename = f"{DAY_NAMES[day.weekday()].lower()}-{day:%Y-%m-%d}.pdf"
        caption = f"☀️ {fmt_day(day)}"

    await update.effective_chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    pdf = await asyncio.to_thread(render)
    await update.message.reply_document(document=BytesIO(pdf), filename=filename, caption=caption)


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nums = parse_nums(context.args)
    if not nums:
        await update.message.reply_html(
            "Usage: <code>/del 3</code> or <code>/del 3 5</code>\nNumbers are in /week\n\n"
            "To stop something repeating: <code>/template del T1</code>"
        )
        return

    chat_id = update.effective_chat.id
    removed, missing = [], []
    with db() as conn:
        for num in nums:
            row = conn.execute(
                "SELECT * FROM events WHERE chat_id = ? AND num = ?", (chat_id, num)
            ).fetchone()
            if not row:
                missing.append(num)
            elif row["template_id"]:
                # запоминаем пропуск, иначе полуночное обновление создаст его снова
                conn.execute("UPDATE events SET cancelled = 1, num = NULL WHERE id = ?", (row["id"],))
                removed.append(row)
            else:
                conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
                removed.append(row)

    unschedule_reminders(context.application, [r["id"] for r in removed])
    lines = []
    for r in removed:
        lines.append(f"🗑 <b>#{r['num']}  {escape(r['title'])}</b> — {fmt_day(date.fromisoformat(r['date']))}")
        if r["template_id"]:
            lines.append("<i>Only this time — it stays in the template and comes back next week.</i>")
    if missing:
        lines.append("Not found: " + ", ".join(f"#{n}" for n in missing) + "  (see /week)")
    await update.message.reply_html("\n".join(lines))


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    usage = (
        "Usage: <code>/remind 3 15</code> — 15 min before #3\n"
        "<code>/remind 3 0</code> — at start  ·  <code>/remind 3 off</code> — no reminder\n\n"
        "For every week: <code>/template remind T1 15</code>"
    )
    nums = parse_nums(args[:1])
    minutes = parse_reminder(args[1]) if len(args) == 2 else None
    if len(args) != 2 or not nums or minutes is None:
        await update.message.reply_html(usage)
        return

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE chat_id = ? AND num = ?", (update.effective_chat.id, nums[0])
        ).fetchone()
        if not row:
            await update.message.reply_html(f"No plan #{nums[0]}. See /week")
            return
        conn.execute("UPDATE events SET reminder_min = ? WHERE id = ?", (minutes, row["id"]))

    schedule_events(context.application, [row["id"]])
    lines = [
        f"<b>#{row['num']}  {escape(row['title'])}</b> — {fmt_day(date.fromisoformat(row['date']))}",
        fmt_reminder(minutes),
    ]
    if row["template_id"]:
        lines.append("<i>Only this time. For every week: /template remind …</i>")
    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------------
# Template commands
# ---------------------------------------------------------------------------

async def template_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub, args = (context.args[0].lower(), context.args[1:]) if context.args else ("", [])
    handler = {
        "add": template_add, "edit": template_edit, "del": template_del,
        "delete": template_del, "remind": template_remind,
    }.get(sub)
    if handler:
        await handler(update, context, args)
    elif not sub:
        await template_show(update)
    else:
        await update.message.reply_html(TEMPLATE_HELP)


async def template_show(update: Update) -> None:
    rows = template_rows(update.effective_chat.id)
    if not rows:
        await update.message.reply_html(f"Your weekly template is empty.\n\n{TEMPLATE_HELP}")
        return
    lines = [f"🔁 <b>Weekly template</b>  ({len(rows)})", ""]
    for t in rows:
        lines += template_lines(t) + [""]
    lines.append("<i>The next 7 days are planned from it automatically — /week\n"
                 "Change: /template edit T1 …  ·  remove: /template del T1</i>")
    await update.message.reply_html("\n".join(lines))


def template_overlaps(conn: sqlite3.Connection, t: sqlite3.Row) -> list[str]:
    others = conn.execute(
        "SELECT * FROM templates WHERE chat_id = ? AND id != ?", (t["chat_id"], t["id"])
    ).fetchall()
    warnings = []
    for o in others:
        shared = sorted(set(row_days(t)) & set(row_days(o)))
        if shared and overlaps(t, o):
            warnings.append(
                f"⚠️ Overlaps with <b>T{o['num']} {escape(o['title'])}</b> ({fmt_days(shared)})"
            )
    return warnings


def planned_note(count: int) -> str:
    if not count:
        return "<i>Nothing falls into the next 7 days yet.</i>"
    return f"📅 Planned {count} time{'s' if count != 1 else ''} in the next 7 days — /week"


async def template_add(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    text = " ".join(args)
    if not text:
        await update.message.reply_html(TEMPLATE_HELP)
        return
    try:
        days, start_time, end_time, title = parse_event(text)
    except ValueError as e:
        await update.message.reply_html(f"⚠️ {e}\n\n{TEMPLATE_HELP}")
        return

    chat_id = update.effective_chat.id
    today = datetime.now(TIMEZONE).date()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO templates (chat_id, num, title, days, start_time, end_time, reminder_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, next_free_num(conn, "templates", chat_id), title,
             ",".join(map(str, days)), start_time, end_time, DEFAULT_REMINDER_MIN),
        )
        t = conn.execute("SELECT * FROM templates WHERE id = ?", (cur.lastrowid,)).fetchone()
        new_ids = fill_plan(conn, today, [t])
        warnings = template_overlaps(conn, t)

    schedule_events(context.application, new_ids)
    lines = ["🔁 Added to the template", ""] + template_lines(t) + ["", planned_note(len(new_ids))]
    if warnings:
        lines += [""] + warnings
    await update.message.reply_html("\n".join(lines))


async def template_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    usage = f"Usage: <code>/template edit T1 monday 10:00-12:00 Math</code>\n\n{FORMAT_HELP}"
    nums = parse_nums(args[:1], prefix="T")
    if not nums or len(args) < 2:
        await update.message.reply_html(usage)
        return
    try:
        days, start_time, end_time, title = parse_event(" ".join(args[1:]))
    except ValueError as e:
        await update.message.reply_html(f"⚠️ {e}\n\n{usage}")
        return

    chat_id = update.effective_chat.id
    today = datetime.now(TIMEZONE).date()
    with db() as conn:
        t = conn.execute(
            "SELECT * FROM templates WHERE chat_id = ? AND num = ?", (chat_id, nums[0])
        ).fetchone()
        if not t:
            await update.message.reply_html(f"No T{nums[0]} in your template. See /template")
            return
        conn.execute(
            "UPDATE templates SET title = ?, days = ?, start_time = ?, end_time = ? WHERE id = ?",
            (title, ",".join(map(str, days)), start_time, end_time, t["id"]),
        )
        # Пересоздаём ближайшие копии; пропущенные через /del остаются пропущенными.
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM events WHERE template_id = ? AND cancelled = 0", (t["id"],)
        )]
        conn.execute("DELETE FROM events WHERE template_id = ? AND cancelled = 0", (t["id"],))
        t = conn.execute("SELECT * FROM templates WHERE id = ?", (t["id"],)).fetchone()
        new_ids = fill_plan(conn, today, [t])
        warnings = template_overlaps(conn, t)

    unschedule_reminders(context.application, old_ids)
    schedule_events(context.application, new_ids)
    lines = ["✏️ Template updated", ""] + template_lines(t) + ["", planned_note(len(new_ids))]
    if warnings:
        lines += [""] + warnings
    await update.message.reply_html("\n".join(lines))


async def template_del(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    nums = parse_nums(args, prefix="T")
    if not nums:
        await update.message.reply_html(
            "Usage: <code>/template del T1</code> or <code>/template del T1 T3</code>\n"
            "Numbers are in /template\n\nTo skip just one time: <code>/del 3</code>"
        )
        return

    chat_id = update.effective_chat.id
    removed, missing, event_ids = [], [], []
    with db() as conn:
        for num in nums:
            t = conn.execute(
                "SELECT * FROM templates WHERE chat_id = ? AND num = ?", (chat_id, num)
            ).fetchone()
            if not t:
                missing.append(num)
                continue
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM events WHERE template_id = ? AND cancelled = 0", (t["id"],)
            )]
            conn.execute("DELETE FROM events WHERE template_id = ?", (t["id"],))
            conn.execute("DELETE FROM templates WHERE id = ?", (t["id"],))
            event_ids += ids
            removed.append((t, len(ids)))

    unschedule_reminders(context.application, event_ids)
    lines = []
    for t, count in removed:
        lines.append(f"🗑 <b>T{t['num']}  {escape(t['title'])}</b> removed from the template")
        if count:
            lines.append(f"<i>and {count} upcoming time{'s' if count != 1 else ''} from your plan</i>")
    if missing:
        lines.append("Not found: " + ", ".join(f"T{n}" for n in missing) + "  (see /template)")
    await update.message.reply_html("\n".join(lines))


async def template_remind(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    nums = parse_nums(args[:1], prefix="T")
    minutes = parse_reminder(args[1]) if len(args) == 2 else None
    if len(args) != 2 or not nums or minutes is None:
        await update.message.reply_html(
            "Usage: <code>/template remind T1 15</code> — 15 min before, every week\n"
            "<code>/template remind T1 0</code> — at start  ·  <code>/template remind T1 off</code>"
        )
        return

    today = datetime.now(TIMEZONE).date()
    with db() as conn:
        t = conn.execute(
            "SELECT * FROM templates WHERE chat_id = ? AND num = ?", (update.effective_chat.id, nums[0])
        ).fetchone()
        if not t:
            await update.message.reply_html(f"No T{nums[0]} in your template. See /template")
            return
        conn.execute("UPDATE templates SET reminder_min = ? WHERE id = ?", (minutes, t["id"]))
        conn.execute(
            "UPDATE events SET reminder_min = ? WHERE template_id = ? AND date >= ?",
            (minutes, t["id"], today.isoformat()),
        )
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM events WHERE template_id = ? AND cancelled = 0", (t["id"],)
        )]

    schedule_events(context.application, ids)
    await update.message.reply_html(
        f"<b>T{t['num']}  {escape(t['title'])}</b>\n{fmt_reminder(minutes)}, every week"
    )


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html("I don't know this command. See /help")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "To add something, start with a command:\n"
        "<code>/template add monday 18:00-19:30 Gym</code> — every week\n"
        "<code>/add friday 15:00 Dentist</code> — just once\n\nAll commands: /help"
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
    app.add_handler(CommandHandler("template", template_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler(["week", "list"], week_cmd))  # /list — по старой памяти
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
