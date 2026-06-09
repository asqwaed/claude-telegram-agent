"""Render small dashboard-style PNG cards for /usage and /context.

Uses Pillow with the DejaVu Sans font that ships with matplotlib (covers
Cyrillic and works headless). Bars are drawn as real rectangles for a clean
look. Every public function returns PNG bytes, or None if rendering is
unavailable, so callers can fall back to a text version.
"""

import logging
from io import BytesIO

import config
from bot import usage as usage_tracker

logger = logging.getLogger(__name__)

# Palette (dark dashboard).
BG = (15, 17, 23)
FG = (230, 233, 239)
MUTED = (138, 145, 158)
ACCENT = (91, 141, 239)
TRACK = (38, 42, 52)
GREEN = (74, 199, 130)
AMBER = (224, 168, 0)
RED = (231, 111, 111)


def _pct_color(pct: float):
    if pct < 50:
        return GREEN
    if pct < 80:
        return AMBER
    return RED


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    from matplotlib.font_manager import FontProperties, findfont

    try:
        fp = FontProperties(
            family="DejaVu Sans", weight="bold" if bold else "normal"
        )
        return ImageFont.truetype(findfont(fp), size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _render(title: str, subtitle: str, rows: list[dict], width: int = 820):
    """rows: {label, value, pct?(0-100), color?}. Returns PNG bytes or None."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    pad, header_h, row_h = 44, 118, 68
    height = header_h + len(rows) * row_h + pad
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_title, f_body, f_small = _font(40, True), _font(30), _font(22)

    d.text((pad, pad), title, font=f_title, fill=ACCENT)
    if subtitle:
        d.text((pad, pad + 50), subtitle, font=f_small, fill=MUTED)

    y = header_h
    for r in rows:
        d.text((pad, y), r["label"], font=f_body, fill=FG)
        val = r.get("value", "")
        vw = d.textlength(val, font=f_body)
        d.text((width - pad - vw, y), val, font=f_body, fill=FG)
        pct = r.get("pct")
        if pct is not None:
            by = y + 42
            d.rounded_rectangle([pad, by, width - pad, by + 12], radius=6, fill=TRACK)
            fill_w = pad + (width - 2 * pad) * min(max(pct, 0), 100) / 100
            if fill_w > pad:
                d.rounded_rectangle(
                    [pad, by, fill_w, by + 12], radius=6,
                    fill=r.get("color") or _pct_color(pct),
                )
        y += row_h

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def usage_card():
    """Token usage + official subscription limits as a card."""
    t = usage_tracker.today_stats()
    a = usage_tracker.total_stats()
    lim = usage_tracker.merged_limits()
    rows: list[dict] = []

    if lim:
        for label, win in (("5ч лимит", "five_hour"), ("недельный", "seven_day")):
            w = lim.get(win)
            if w and w.get("pct") is not None:
                pct = float(w["pct"])
                rows.append({"label": label, "value": f"{round(pct)}%", "pct": pct})
    else:
        rows.append({"label": "лимиты Claude", "value": "нет данных"})

    today_total = t["input"] + t["output"] + t["cache_read"] + t["cache_creation"]

    def k(n):
        return usage_tracker._fmt(n)

    rows.append({"label": "вход (сегодня)", "value": k(t["input"]),
                 "pct": 100 * t["input"] / today_total if today_total else 0,
                 "color": ACCENT})
    rows.append({"label": "выход (сегодня)", "value": k(t["output"]),
                 "pct": 100 * t["output"] / today_total if today_total else 0,
                 "color": RED})
    rows.append({"label": "кэш (сегодня)", "value": k(t["cache_read"] + t["cache_creation"])})
    subtitle = (f"сегодня: {t['requests']} запросов · ≈${t['cost']:.2f}   |   "
                f"всего: {a['requests']} · ≈${a['cost']:.2f}")
    return _render("Token Usage", subtitle, rows)


def context_card(stats: dict):
    """Conversation context fullness as a card."""
    pct = stats["percent"]
    rows = [
        {"label": "заполнено", "value": f"{pct}%", "pct": pct},
        {"label": "сообщений", "value": str(stats["messages"])},
        {"label": "токенов", "value": f"{stats['tokens']} / {stats['limit']}"},
        {"label": "из них профиль", "value": f"~{stats['profile_tokens']}"},
    ]
    sub = "сожмётся автоматически при переполнении"
    return _render("Контекст диалога", sub, rows)
