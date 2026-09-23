"""PDF calendar rendering: a weekly grid (landscape) or a single-day timeline (portrait)."""

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

# Встроенные шрифты PDF не умеют кириллицу, поэтому везём свой (PT Sans, OFL).
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
pdfmetrics.registerFont(TTFont("PTSans", os.path.join(_FONT_DIR, "PT_Sans-Web-Regular.ttf")))
pdfmetrics.registerFont(TTFont("PTSans-Bold", os.path.join(_FONT_DIR, "PT_Sans-Web-Bold.ttf")))
REGULAR, BOLD = "PTSans", "PTSans-Bold"
_GLYPHS = pdfmetrics.getFont(REGULAR).face.charToGlyph

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

INK = HexColor("#111827")
MUTED = HexColor("#6B7280")
FAINT = HexColor("#9CA3AF")
GRID = HexColor("#E5E7EB")
GRID_SOFT = HexColor("#F1F2F4")
ACCENT = HexColor("#4F46E5")
TODAY_TINT = HexColor("#F5F6FF")

# (accent bar, block fill, text) — одинаковые названия всегда одного цвета
PALETTE = [
    tuple(HexColor(c) for c in triple)
    for triple in [
        ("#4F46E5", "#E0E7FF", "#312E81"),  # indigo
        ("#0EA5E9", "#E0F2FE", "#0C4A6E"),  # sky
        ("#10B981", "#D1FAE5", "#064E3B"),  # emerald
        ("#F59E0B", "#FEF3C7", "#78350F"),  # amber
        ("#F43F5E", "#FFE4E6", "#881337"),  # rose
        ("#8B5CF6", "#EDE9FE", "#4C1D95"),  # violet
        ("#14B8A6", "#CCFBF1", "#134E4A"),  # teal
        ("#F97316", "#FFEDD5", "#7C2D12"),  # orange
    ]
]

MARGIN = 32
GUTTER = 36  # колонка с подписями часов
HEADER_H = 62


@dataclass
class Event:
    title: str
    day: date
    start: int  # minutes from midnight
    end: int    # == start for a point-in-time event ("8:00 Wake up")

    @property
    def is_point(self) -> bool:
        return self.start == self.end

    @property
    def stop(self) -> int:
        """End minute for drawing; events that cross midnight are cut at 24:00."""
        if self.is_point:
            return self.start
        return self.end if self.end > self.start else 24 * 60


@dataclass
class _Timeline:
    top: float
    bottom: float
    first_hour: int
    last_hour: int

    @property
    def ppm(self) -> float:
        """Points per minute."""
        return (self.top - self.bottom) / ((self.last_hour - self.first_hour) * 60)

    def y(self, minute: float) -> float:
        return self.top - (minute - self.first_hour * 60) * self.ppm


@dataclass
class _Slot:
    event: Event
    start: float  # minutes
    end: float    # visual end in minutes (short events are stretched to stay readable)
    lane: int = 0
    lanes: int = 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_week(events: list[Event], first_day: date, today: date, brand: str,
                generated_at: datetime) -> bytes:
    """Seven days starting from first_day (not necessarily a Monday)."""
    width, height = landscape(A4)
    buf = BytesIO()
    c = Canvas(buf, pagesize=(width, height))
    c.setTitle("Weekly schedule")
    c.setAuthor(brand)

    days = [first_day + timedelta(days=i) for i in range(7)]
    events = [e for e in events if e.day in days]
    _header(c, width, height, "Weekly schedule", _range_label(days[0], days[-1]), _summary(events))

    col_head = 34
    left, right = MARGIN + GUTTER, width - MARGIN
    col_w = (right - left) / 7
    tl = _Timeline(
        top=height - MARGIN - HEADER_H - col_head,
        bottom=MARGIN + 14,
        **_hour_range(events),
    )

    for i, day in enumerate(days):
        x = left + i * col_w
        is_today = day == today
        if is_today:
            c.setFillColor(TODAY_TINT)
            c.rect(x, tl.bottom, col_w, tl.top - tl.bottom + col_head, stroke=0, fill=1)
        c.setFillColor(ACCENT if is_today else INK)
        c.setFont(BOLD, 10.5)
        c.drawString(x + 6, tl.top + col_head - 15, DAY_NAMES[day.weekday()])
        c.setFillColor(ACCENT if is_today else MUTED)
        c.setFont(REGULAR, 8.5)
        label = f"{day.day} {MONTH_NAMES[day.month - 1][:3]}" + ("  ·  today" if is_today else "")
        c.drawString(x + 6, tl.top + col_head - 27, label)

    _grid(c, tl, left, right)
    c.setStrokeColor(GRID)
    c.setLineWidth(0.6)
    for i in range(8):
        x = left + i * col_w
        c.line(x, tl.bottom, x, tl.top + col_head)

    colors = _assign_colors(events)
    for i, day in enumerate(days):
        day_events = [e for e in events if e.day == day]
        for slot in _layout(day_events, tl, min_h=15, point_h=13):
            lane_w = col_w / slot.lanes
            x = left + i * col_w + slot.lane * lane_w + 2
            _draw_slot(c, slot, x, lane_w - 4, tl, colors[_key(slot.event)],
                       title_size=8.5, with_duration=False)

    _footer(c, width, brand, generated_at)
    c.showPage()
    c.save()
    return buf.getvalue()


def render_day(events: list[Event], day: date, today: date, brand: str,
               generated_at: datetime) -> bytes:
    width, height = A4
    buf = BytesIO()
    c = Canvas(buf, pagesize=(width, height))
    weekday = day.weekday()
    c.setTitle(f"{DAY_NAMES[weekday]} schedule")
    c.setAuthor(brand)

    day_events = [e for e in events if e.day == day]
    subtitle = f"{day.day} {MONTH_NAMES[day.month - 1]} {day.year}"
    if day == today:
        subtitle += "  ·  Today"
    elif day == today + timedelta(days=1):
        subtitle += "  ·  Tomorrow"
    _header(c, width, height, DAY_NAMES[weekday], subtitle,
            _summary(day_events))

    left, right = MARGIN + GUTTER + 4, width - MARGIN
    tl = _Timeline(
        top=height - MARGIN - HEADER_H - 14,
        bottom=MARGIN + 14,
        **_hour_range(day_events),
    )
    _grid(c, tl, left, right)

    if not day_events:
        text = "Nothing planned — enjoy your free day."
        text_w = pdfmetrics.stringWidth(text, REGULAR, 14)
        mid_x, mid_y = (left + right) / 2, tl.y((tl.first_hour + tl.last_hour) * 30 + 30)
        c.setFillColor(HexColor("#FFFFFF"))
        c.rect(mid_x - text_w / 2 - 12, mid_y - 10, text_w + 24, 26, stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.setFont(REGULAR, 14)
        c.drawCentredString(mid_x, mid_y, text)

    colors = _assign_colors(events)
    for slot in _layout(day_events, tl, min_h=22, point_h=20):
        lane_w = (right - left) / slot.lanes
        x = left + slot.lane * lane_w + 3
        _draw_slot(c, slot, x, lane_w - 6, tl, colors[_key(slot.event)],
                   title_size=12, with_duration=True)

    _footer(c, width, brand, generated_at)
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _hour_range(events: list[Event]) -> dict:
    """Visible hours: from the earliest start to the latest end, at least 8 hours."""
    if not events:
        return {"first_hour": 8, "last_hour": 20}
    first = min(e.start for e in events) // 60
    last = min(24, math.ceil(max(max(e.stop, e.start + 30) for e in events) / 60))
    while last - first < 8:
        if last < 24:
            last += 1
        else:
            first -= 1
    return {"first_hour": first, "last_hour": last}


def _layout(events: list[Event], tl: _Timeline, min_h: float, point_h: float) -> list[_Slot]:
    """Put overlapping events side by side, like Google Calendar does."""
    slots = []
    for e in events:
        if e.is_point:
            end = e.start + point_h / tl.ppm
        else:
            end = max(e.stop, e.start + min_h / tl.ppm)
        slots.append(_Slot(e, e.start, end))
    slots.sort(key=lambda s: (s.start, -s.end))

    cluster: list[_Slot] = []
    lane_ends: list[float] = []
    cluster_end = -1.0

    def close_cluster():
        for s in cluster:
            s.lanes = len(lane_ends)

    for s in slots:
        if cluster and s.start >= cluster_end:
            close_cluster()
            cluster, lane_ends = [], []
        for i, lane_end in enumerate(lane_ends):
            if lane_end <= s.start:
                s.lane, lane_ends[i] = i, s.end
                break
        else:
            s.lane = len(lane_ends)
            lane_ends.append(s.end)
        cluster.append(s)
        cluster_end = max(cluster_end, s.end)
    if cluster:
        close_cluster()
    return slots


def _assign_colors(events: list[Event]) -> dict:
    colors = {}
    for e in sorted(events, key=lambda e: (e.day, e.start)):
        colors.setdefault(_key(e), PALETTE[len(colors) % len(PALETTE)])
    return colors


def _key(e: Event) -> str:
    return e.title.casefold()


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _header(c: Canvas, width: float, height: float, title: str, subtitle: str, right: str) -> None:
    top = height - MARGIN
    c.setFillColor(ACCENT)
    c.roundRect(MARGIN, top - 4, 28, 4, 2, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont(BOLD, 24)
    c.drawString(MARGIN, top - 30, title)
    c.setFillColor(MUTED)
    c.setFont(REGULAR, 11.5)
    c.drawString(MARGIN, top - 48, subtitle)
    c.setFont(REGULAR, 10)
    c.drawRightString(width - MARGIN, top - 30, right)


def _footer(c: Canvas, width: float, brand: str, generated_at: datetime) -> None:
    c.setFillColor(FAINT)
    c.setFont(REGULAR, 7.5)
    stamp = f"{generated_at.day} {MONTH_NAMES[generated_at.month - 1][:3]} {generated_at:%Y, %H:%M}"
    c.drawString(MARGIN, MARGIN - 8, _clean(brand) or "Schedule")
    c.drawRightString(width - MARGIN, MARGIN - 8, f"Generated {stamp}")


def _grid(c: Canvas, tl: _Timeline, x0: float, x1: float) -> None:
    half_hours = tl.ppm * 30 >= 14
    for hour in range(tl.first_hour, tl.last_hour + 1):
        y = tl.y(hour * 60)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.6)
        c.line(x0, y, x1, y)
        c.setFillColor(FAINT)
        c.setFont(REGULAR, 7.5)
        c.drawRightString(x0 - 6, y - 2.6, f"{hour:02d}:00")
        if half_hours and hour < tl.last_hour:
            y_half = tl.y(hour * 60 + 30)
            c.setStrokeColor(GRID_SOFT)
            c.setDash(2, 2)
            c.line(x0, y_half, x1, y_half)
            c.setDash()


def _draw_slot(c: Canvas, slot: _Slot, x: float, w: float, tl: _Timeline, color: tuple,
               title_size: float, with_duration: bool) -> None:
    bar, fill, ink = color
    e = slot.event
    y_top = tl.y(slot.start) - 1
    h = min(tl.y(slot.start) - tl.y(slot.end), y_top - tl.bottom) - 2
    title = _clean(e.title) or "Untitled"
    time_size = title_size - 1.5

    if e.is_point:
        # маленькая «таблетка» с точкой: 08:00 Wake up
        c.setFillColor(fill)
        c.roundRect(x, y_top - h, w, h, h / 2, stroke=0, fill=1)
        c.setFillColor(bar)
        c.circle(x + h / 2, y_top - h / 2, h * 0.17, stroke=0, fill=1)
        _one_line(c, x + h * 0.9, y_top - h / 2, w - h * 0.9 - 4, _fmt(e.start), title,
                  ink, title_size, time_size)
        return

    c.setFillColor(fill)
    c.roundRect(x, y_top - h, w, h, 4, stroke=0, fill=1)
    c.setFillColor(bar)
    c.roundRect(x, y_top - h, 3.2, h, 1.6, stroke=0, fill=1)

    narrow = w < 56  # несколько пересекающихся событий в колонке недели
    if narrow:
        title_size, time_size = title_size - 1, time_size - 1
    text_x = x + (6 if narrow else 8)
    text_w = w - (text_x - x) - 3
    if with_duration:
        time_text = f"{_fmt(e.start)} – {_fmt(e.end)}  ·  {_duration(e.stop - e.start)}"
    else:
        time_text = f"{_fmt(e.start)}–{_fmt(e.end)}"
    if pdfmetrics.stringWidth(time_text, REGULAR, time_size) > text_w:
        time_text = _fmt(e.start)
    lead_title, lead_time = title_size * 1.18, time_size * 1.3
    room = h - 6

    if room < lead_title + lead_time:
        _one_line(c, text_x, y_top - h / 2, text_w, _fmt(e.start), title, ink, title_size, time_size)
        return

    # в узкой колонке название важнее времени — отдаём ему всё место
    show_time = not narrow or room >= 3 * lead_title + lead_time
    max_lines = max(1, int((room - (lead_time if show_time else 0)) // lead_title))
    y = y_top - 3 - title_size * 0.95
    c.setFillColor(ink)
    c.setFont(BOLD, title_size)
    for line in _wrap(title, BOLD, title_size, text_w, max_lines):
        c.drawString(text_x, y, line)
        y -= lead_title
    if show_time:
        c.setFillAlpha(0.72)
        c.setFont(REGULAR, time_size)
        c.drawString(text_x, y + lead_title - lead_time, _fit(time_text, REGULAR, time_size, text_w))
        c.setFillAlpha(1)


def _one_line(c: Canvas, x: float, y_mid: float, w: float, time_text: str, title: str,
              ink, title_size: float, time_size: float) -> None:
    """'10:00  Title' vertically centred on y_mid; the time is dropped if the title wouldn't fit."""
    baseline = y_mid - title_size * 0.34
    c.setFillColor(ink)
    shift = pdfmetrics.stringWidth(time_text + "  ", REGULAR, time_size)
    if shift + pdfmetrics.stringWidth(title, BOLD, title_size) > w:
        shift = 0
    else:
        c.setFillAlpha(0.72)
        c.setFont(REGULAR, time_size)
        c.drawString(x, baseline, time_text)
        c.setFillAlpha(1)
    c.setFont(BOLD, title_size)
    c.drawString(x + shift, baseline, _fit(title, BOLD, title_size, w - shift))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Drop characters the font can't draw (emoji etc.) instead of printing empty boxes."""
    kept = "".join(ch for ch in text if ord(ch) in _GLYPHS or ch.isspace())
    return " ".join(kept.split())


def _fit(text: str, font: str, size: float, max_w: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= max_w:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > max_w:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


def _wrap(text: str, font: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines = []
    for line in simpleSplit(text, font, size, max_w):
        # слово шире колонки режем по буквам, а не превращаем в «Вст…»
        while pdfmetrics.stringWidth(line, font, size) > max_w and len(line) > 1:
            cut = len(line) - 1
            while cut > 1 and pdfmetrics.stringWidth(line[:cut], font, size) > max_w:
                cut -= 1
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and pdfmetrics.stringWidth(last + "…", font, size) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


def _fmt(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def _summary(events: list[Event]) -> str:
    count = len(events)
    if not count:
        return "No events"
    busy = sum(e.stop - e.start for e in events)
    text = f"{count} event{'s' if count != 1 else ''}"
    return text + (f"  ·  {_duration(busy)} planned" if busy else "")


def _range_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day} – {end.day} {MONTH_NAMES[end.month - 1]} {end.year}"
    if start.year == end.year:
        return (f"{start.day} {MONTH_NAMES[start.month - 1]} – "
                f"{end.day} {MONTH_NAMES[end.month - 1]} {end.year}")
    return (f"{start.day} {MONTH_NAMES[start.month - 1]} {start.year} – "
            f"{end.day} {MONTH_NAMES[end.month - 1]} {end.year}")
