"""Token-usage tracking for the bot.

Every Claude Code turn appends a JSON line to ``memory/usage.jsonl`` with its
token counts and estimated cost. This module aggregates those records for the
``/usage`` command — a text infographic plus an optional per-day PNG chart.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

USAGE_FILE = config.MEMORY_DIR / "usage.jsonl"
# Latest live rate-limit info captured from the stream-json ``rate_limit_event``
# (one entry per window). Fresher than the statusline snapshot.
LIVE_LIMITS_FILE = config.MEMORY_DIR / "ratelimit_live.json"


# --- recording -------------------------------------------------------------
def record(chat_id: int, usage: dict) -> None:
    """Append one usage record (best-effort; never raises into the caller)."""
    if not usage:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "input": usage.get("input", 0),
        "output": usage.get("output", 0),
        "cache_read": usage.get("cache_read", 0),
        "cache_creation": usage.get("cache_creation", 0),
        "cost": usage.get("cost", 0.0),
        "duration_ms": usage.get("duration_ms", 0),
        "turns": usage.get("turns", 0),
    }
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to write usage record: %s", exc)


# --- reading / aggregation -------------------------------------------------
def _load() -> list[dict]:
    if not USAGE_FILE.exists():
        return []
    rows = []
    try:
        for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as exc:
        logger.warning("Failed to read usage file: %s", exc)
    return rows


def _local_day(row: dict) -> str:
    """Local YYYY-MM-DD for a record's UTC timestamp."""
    try:
        dt = datetime.fromisoformat(row["ts"]).astimezone()
        return dt.strftime("%Y-%m-%d")
    except (KeyError, ValueError):
        return "?"


def _sum(rows: list[dict]) -> dict:
    agg = {"requests": len(rows), "input": 0, "output": 0,
           "cache_read": 0, "cache_creation": 0, "cost": 0.0}
    for r in rows:
        agg["input"] += r.get("input", 0)
        agg["output"] += r.get("output", 0)
        agg["cache_read"] += r.get("cache_read", 0)
        agg["cache_creation"] += r.get("cache_creation", 0)
        agg["cost"] += r.get("cost", 0.0)
    return agg


def today_stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    return _sum([r for r in _load() if _local_day(r) == today])


def total_stats() -> dict:
    return _sum(_load())


def daily_series(days: int = 14) -> list[tuple[str, int, int]]:
    """Return [(YYYY-MM-DD, input+cache, output)] for the last ``days`` days."""
    rows = _load()
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        day_rows = [r for r in rows if _local_day(r) == day]
        agg = _sum(day_rows)
        out.append((day, agg["input"] + agg["cache_read"] + agg["cache_creation"],
                    agg["output"]))
    return out


# --- formatting ------------------------------------------------------------
def _fmt(n: int) -> str:
    """Compact token count: 1234 -> 1.2k, 2_100_000 -> 2.1M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _bar(value: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * value / total)
    return "█" * filled + "░" * (width - filled)


# --- official subscription limits (from the statusline snapshot) -----------
def _eta(resets_at) -> str:
    """Human ETA until a unix-epoch reset time, e.g. '2h10m' / '3d'."""
    try:
        secs = int(resets_at) - int(datetime.now().timestamp())
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return "скоро"
    d, rem = divmod(secs, 86400)
    h, m = divmod(rem % 86400, 3600)[0], (rem % 3600) // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def official_limits() -> dict | None:
    """Read the statusline rate-limit snapshot, or None if absent/unpopulated."""
    p = config.RATELIMITS_FILE
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    fh, sd = d.get("five_hour"), d.get("seven_day")
    if not fh and not sd:
        return None
    cap = d.get("captured_at")
    age_min = None
    if cap:
        try:
            age_min = max(0, int((datetime.now().timestamp() - float(cap)) / 60))
        except (TypeError, ValueError):
            age_min = None
    return {"five_hour": fh, "seven_day": sd, "age_min": age_min}


# --- live subscription limits (from the stream-json rate_limit_event) ------
def record_live_limit(info: dict) -> None:
    """Persist one ``rate_limit_info`` from a stream event (best-effort).

    Stored per window (five_hour / seven_day) with a capture timestamp so the
    freshest source can be chosen later. Never raises into the caller.
    """
    rtype = info.get("rateLimitType")
    if not rtype:
        return
    try:
        data: dict = {}
        if LIVE_LIMITS_FILE.exists():
            try:
                data = json.loads(LIVE_LIMITS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                data = {}
        now = datetime.now().timestamp()
        data[rtype] = {
            "utilization": info.get("utilization"),
            "status": info.get("status"),
            "resetsAt": info.get("resetsAt"),
            "isUsingOverage": info.get("isUsingOverage"),
            "ts": now,
        }
        data["updated_at"] = now
        LIVE_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIVE_LIMITS_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Failed to write live limit: %s", exc)


def live_limits() -> dict | None:
    """Read the live rate-limit cache, or None if absent/unreadable."""
    if not LIVE_LIMITS_FILE.exists():
        return None
    try:
        return json.loads(LIVE_LIMITS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _norm_live(e: dict | None) -> dict | None:
    if not e:
        return None
    u = e.get("utilization")
    try:
        pct = round(float(u) * 100) if u is not None else None
    except (TypeError, ValueError):
        pct = None
    return {"pct": pct, "status": e.get("status"), "resets_at": e.get("resetsAt"),
            "ts": e.get("ts"), "source": "stream"}


def _norm_official(e: dict | None, cap) -> dict | None:
    if not e:
        return None
    try:
        pct = round(float(e.get("used_percentage", 0)))
    except (TypeError, ValueError):
        pct = None
    return {"pct": pct, "status": None, "resets_at": e.get("resets_at"),
            "ts": cap, "source": "statusline"}


def merged_limits() -> dict | None:
    """Per-window limits merging the live stream events and the statusline
    snapshot, preferring whichever window was captured more recently.

    Returns ``{"five_hour": {...}|None, "seven_day": {...}|None}`` where each
    entry has ``pct, status, resets_at, ts, source`` — or None if no source has
    any data at all.
    """
    live = live_limits() or {}
    off_raw: dict = {}
    p = config.RATELIMITS_FILE
    if p.exists():
        try:
            off_raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            off_raw = {}
    off_cap = off_raw.get("captured_at")

    out: dict = {}
    for win in ("five_hour", "seven_day"):
        cands = [c for c in (_norm_live(live.get(win)),
                             _norm_official(off_raw.get(win), off_cap)) if c]
        if not cands:
            out[win] = None
            continue
        cands.sort(key=lambda c: c.get("ts") or 0, reverse=True)
        out[win] = cands[0]
    if out["five_hour"] is None and out["seven_day"] is None:
        return None
    return out


def _status_icon(status: str | None) -> str:
    if not status:
        return ""
    s = status.lower()
    if "reject" in s or "throttl" in s:
        return " 🛑"
    if "warning" in s:
        return " ⚠️"
    if "allow" in s:
        return " ✅"
    return ""


def _limit_line(label: str, w: dict | None) -> str | None:
    if not w:
        return None
    pct = w.get("pct")
    eta = _eta(w.get("resets_at"))
    eta_s = f"  сброс {eta}" if eta else ""
    pct_s = f"{pct}%" if pct is not None else "—"
    return f"{label} {_bar(pct or 0, 100)} {pct_s}{_status_icon(w.get('status'))}{eta_s}"


def limits_block() -> str:
    """HTML block with the 5h + weekly limits (or a hint if missing)."""
    lim = merged_limits()
    if lim is None:
        return (
            "🔋 <b>Лимиты Claude</b>\n"
            "<i>нет данных — отправь любой запрос (стрим подтянет свежий статус) "
            "или открой Claude Code интерактивно</i>"
        )
    lines = ["🔋 <b>Лимиты Claude</b>"]
    newest_ts = 0.0
    for label, win in (("5ч ", "five_hour"), ("нед", "seven_day")):
        line = _limit_line(label, lim.get(win))
        if line:
            lines.append(line)
            w = lim[win]
            if w.get("ts"):
                newest_ts = max(newest_ts, float(w["ts"]))
    if newest_ts:
        age_min = max(0, int((datetime.now().timestamp() - newest_ts) / 60))
        fresh = "только что" if age_min == 0 else f"{age_min} мин назад"
        lines.append(f"<i>обновлено {fresh}</i>")
    return "\n".join(lines)


def infographic() -> str:
    """Build the HTML text infographic for /usage."""
    t = today_stats()
    a = total_stats()
    today_total = t["input"] + t["output"] + t["cache_read"] + t["cache_creation"]
    # Proportional bars for today (input vs output vs cache).
    lines = [
        limits_block(),
        "",
        "📊 <b>Token Usage</b> <i>(этот бот)</i>",
        "",
        f"<b>сегодня</b> · запросов: {t['requests']}",
        f"вход   {_bar(t['input'], today_total)} {_fmt(t['input'])}",
        f"выход  {_bar(t['output'], today_total)} {_fmt(t['output'])}",
        f"кэш    {_bar(t['cache_read'] + t['cache_creation'], today_total)} "
        f"{_fmt(t['cache_read'] + t['cache_creation'])}",
        f"≈ ${t['cost']:.2f}",
        "",
        "<b>всего</b>",
        f"запросов: {a['requests']} · вход {_fmt(a['input'])} · "
        f"выход {_fmt(a['output'])} · кэш {_fmt(a['cache_read'] + a['cache_creation'])}",
        f"≈ ${a['cost']:.2f}",
        "",
        "<i>оценка по API-расценкам; на подписке не списывается. /usage chart — график</i>",
    ]
    return "\n".join(lines)


def render_chart(days: int = 14) -> bytes | None:
    """Render a stacked per-day token bar chart as PNG bytes, or None if unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    series = daily_series(days)
    labels = [d[5:] for d, _, _ in series]  # MM-DD
    inp = [i for _, i, _ in series]
    out = [o for _, _, o in series]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(labels, inp, label="вход+кэш", color="#5b8def")
    ax.bar(labels, out, bottom=inp, label="выход", color="#ef6f6f")
    ax.set_title(f"Token usage — последние {days} дней")
    ax.set_ylabel("токены")
    ax.legend(loc="upper left", fontsize=8)
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    fig.tight_layout()

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()
