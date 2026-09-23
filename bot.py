import logging
import os
import re
import sqlite3
import zoneinfo
from datetime import datetime, time as dtime
from html import escape
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# accepted aliases -> weekday index (Monday = 0)
DAY_ALIASES = {
    "mon": 0, "mo": 0, "monday": 0, "пн": 0, "понедельник": 0,
    "tue": 1, "tu": 1, "tuesday": 1, "вт": 1, "вторник": 1,
    "wed": 2, "we": 2, "wednesday": 2, "ср": 2, "среда": 2,
    "thu": 3, "th": 3, "thursday": 3, "чт": 3, "четверг": 3,
    "fri": 4, "fr": 4, "friday": 4, "пт": 4, "пятница": 4,
    "sat": 5, "sa": 5, "saturday": 5, "сб": 5, "суббота": 5,
    "sun": 6, "su": 6, "sunday": 6, "вс": 6, "воскресенье": 6,
}

TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                end_time TEXT NOT NULL,    -- HH:MM
                reminder_min INTEGER NOT NULL DEFAULT {default_min}
            )
            """.format(default_min=DEFAULT_REMINDER_MIN)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_days(raw: str) -> list[int]:
    """'mon,tue,wed' or 'пн,ср,пт' or 'weekdays' or 'daily' -> sorted list of ints."""
    raw = raw.strip().lower()
    if raw in ("daily", "everyday", "все", "ежедневно"):
        return list(range(7))
    if raw in ("weekdays", "будни"):
        return [0, 1, 2, 3, 4]
    if raw in ("weekend", "выходные"):
        return [5, 6]

    days = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk not in DAY_ALIASES:
            raise ValueError(f"Не понял день: '{chunk}'")
        days.add(DAY_ALIASES[chunk])
    if not days:
        raise ValueError("Не указаны дни")
    return sorted(days)


def parse_time_range(raw: str) -> tuple[str, str]:
    m = TIME_RANGE_RE.match(raw.strip())
    if not m:
        raise ValueError("Формат времени должен быть ЧЧ:ММ-ЧЧ:ММ, например 10:00-12:00")
    sh, sm, eh, em = m.groups()
    if int(sh) > 23 or int(eh) > 23 or int(sm) > 59 or int(em) > 59:
        raise ValueError("Некорректное время")
    return f"{int(sh):02d}:{sm}", f"{int(eh):02d}:{em}"


def fmt_days(days_csv: str) -> str:
    idxs = [int(d) for d in days_csv.split(",")]
    if idxs == list(range(7)):
        return "ежедневно"
    if idxs == [0, 1, 2, 3, 4]:
        return "будни"
    if idxs == [5, 6]:
        return "выходные"
    return ", ".join(DAY_NAMES_RU[i] for i in idxs)


# ---------------------------------------------------------------------------
# Reminder scheduling
# ---------------------------------------------------------------------------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    text = (
        f"⏰ Через {data['reminder_min']} мин: <b>{escape(data['title'])}</b>\n"
        f"{data['start_time']}–{data['end_time']}"
    )
    await context.bot.send_message(chat_id=data["chat_id"], text=text, parse_mode=ParseMode.HTML)


def reminder_schedule(start_hhmm: str, minutes: int, days: list[int]) -> tuple[dtime, tuple[int, ...]]:
    """Время напоминания и дни недели в формате PTB.

    У нас дни: Пн=0 ... Вс=6 (как datetime.weekday()).
    В PTB 20+ run_daily считает дни как cron: Вс=0, Пн=1 ... Сб=6.
    Если напоминание уходит за полночь (занятие в 00:05, напоминание за 10 мин),
    оно должно сработать в предыдущий день.
    """
    h, m = map(int, start_hhmm.split(":"))
    day_shift, total = divmod(h * 60 + m - minutes, 24 * 60)  # day_shift <= 0
    remind_at = dtime(hour=total // 60, minute=total % 60, tzinfo=TIMEZONE)
    ptb_days = tuple(sorted({(d + day_shift + 1) % 7 for d in days}))
    return remind_at, ptb_days


def schedule_event_jobs(app: Application, event: sqlite3.Row) -> None:
    unschedule_event_jobs(app, event["id"])

    days = [int(d) for d in event["days"].split(",")]
    remind_at, ptb_days = reminder_schedule(event["start_time"], event["reminder_min"], days)
    app.job_queue.run_daily(
        send_reminder,
        time=remind_at,
        days=ptb_days,
        name=f"reminder_{event['id']}",
        data={
            "chat_id": event["chat_id"],
            "title": event["title"],
            "start_time": event["start_time"],
            "end_time": event["end_time"],
            "reminder_min": event["reminder_min"],
        },
    )


def unschedule_event_jobs(app: Application, event_id: int) -> None:
    job_name = f"reminder_{event_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()


async def load_all_jobs(app: Application) -> None:
    with db() as conn:
        rows = conn.execute("SELECT * FROM events").fetchall()
    for row in rows:
        schedule_event_jobs(app, row)
    logger.info("Scheduled reminders for %d events", len(rows))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = """
<b>Команды</b>

/add ДНИ ЧЧ:ММ-ЧЧ:ММ Название — добавить занятие
   Дни: mon,tue,wed,thu,fri,sat,sun (или пн,вт,ср...) / weekdays / weekend / daily
   Пример: <code>/add mon,tue,wed,thu,fri 10:00-12:00 Java Backend</code>
   Пример: <code>/add сб 08:30-09:30 Training</code>

/today — расписание на сегодня
/week — расписание на всю неделю
/list — список всех занятий с ID (для удаления)
/del ID — удалить занятие
/remind ID МИНУТЫ — изменить время напоминания (за сколько минут до начала)
/help — эта справка
""".strip()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        f"Привет! Я буду хранить твоё расписание и присылать напоминания.\n\n{HELP_TEXT}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 3:
        await update.message.reply_html(
            "Использование:\n<code>/add ДНИ ЧЧ:ММ-ЧЧ:ММ Название</code>\n"
            "Пример: <code>/add mon,tue,wed,thu,fri 10:00-12:00 Java Backend</code>"
        )
        return

    days_raw, time_raw = args[0], args[1]
    title = " ".join(args[2:]).strip()

    try:
        days = parse_days(days_raw)
        start_time, end_time = parse_time_range(time_raw)
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    if not title:
        await update.message.reply_text("Название не может быть пустым")
        return

    chat_id = update.effective_chat.id
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO events (chat_id, title, days, start_time, end_time, reminder_min) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, title, ",".join(map(str, days)), start_time, end_time, DEFAULT_REMINDER_MIN),
        )
        conn.commit()
        event_id = cur.lastrowid
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    schedule_event_jobs(context.application, row)

    await update.message.reply_html(
        f"✅ Добавлено (ID {event_id}): <b>{escape(title)}</b>\n"
        f"{fmt_days(row['days'])}, {start_time}–{end_time}\n"
        f"Напоминание за {DEFAULT_REMINDER_MIN} мин до начала."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY start_time", (chat_id,)
        ).fetchall()

    if not rows:
        await update.message.reply_text("Расписание пустое. Добавь занятие через /add")
        return

    lines = ["<b>Все занятия:</b>"]
    for r in rows:
        lines.append(
            f"#{r['id']} — <b>{escape(r['title'])}</b> — {fmt_days(r['days'])}, "
            f"{r['start_time']}–{r['end_time']} (напом. за {r['reminder_min']} мин)"
        )
    await update.message.reply_html("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    weekday = datetime.now(TIMEZONE).weekday()  # Monday = 0

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY start_time", (chat_id,)
        ).fetchall()

    todays = [r for r in rows if str(weekday) in r["days"].split(",")]
    if not todays:
        await update.message.reply_html(f"На сегодня ({DAY_NAMES_RU[weekday]}) ничего нет 🎉")
        return

    lines = [f"<b>Сегодня, {DAY_NAMES_RU[weekday]}:</b>"]
    for r in todays:
        lines.append(f"{r['start_time']}–{r['end_time']} — {escape(r['title'])}")
    await update.message.reply_html("\n".join(lines))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY start_time", (chat_id,)
        ).fetchall()

    if not rows:
        await update.message.reply_text("Расписание пустое. Добавь занятие через /add")
        return

    lines = []
    for day_idx in range(7):
        day_events = [r for r in rows if str(day_idx) in r["days"].split(",")]
        lines.append(f"\n<b>{DAY_NAMES_RU[day_idx]}</b>")
        if not day_events:
            lines.append("—")
        else:
            for r in day_events:
                lines.append(f"{r['start_time']}–{r['end_time']} — {escape(r['title'])}")
    await update.message.reply_html("\n".join(lines))


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Использование: /del ID   (узнать ID: /list)")
        return

    event_id = int(args[0])
    chat_id = update.effective_chat.id
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ? AND chat_id = ?", (event_id, chat_id)
        ).fetchone()
        if not row:
            await update.message.reply_text("Занятие с таким ID не найдено")
            return
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()

    unschedule_event_jobs(context.application, event_id)
    await update.message.reply_html(f"🗑 Удалено: <b>{escape(row['title'])}</b>")


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Использование: /remind ID МИНУТЫ   например /remind 3 15")
        return

    event_id, minutes = int(args[0]), int(args[1])
    if minutes > MAX_REMINDER_MIN:
        await update.message.reply_text(f"Максимум {MAX_REMINDER_MIN} минут (сутки)")
        return

    chat_id = update.effective_chat.id
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ? AND chat_id = ?", (event_id, chat_id)
        ).fetchone()
        if not row:
            await update.message.reply_text("Занятие с таким ID не найдено")
            return
        conn.execute("UPDATE events SET reminder_min = ? WHERE id = ?", (minutes, event_id))
        conn.commit()
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    schedule_event_jobs(context.application, row)
    await update.message.reply_html(
        f"🔔 Напоминание для <b>{escape(row['title'])}</b> теперь за {minutes} мин до начала."
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

    app = Application.builder().token(BOT_TOKEN).post_init(load_all_jobs).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))

    logger.info(
        "Bot starting (timezone=%s, local time %s, db=%s)...",
        TIMEZONE,
        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
        DB_PATH,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
