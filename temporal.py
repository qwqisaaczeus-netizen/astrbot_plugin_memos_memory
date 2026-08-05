"""Temporal semantics shared by compression, indexing, recall and WebUI.

The plugin deals with four different clocks:
- event time: when the remembered scene happened;
- source create/update time: when Memos stored or edited the memo;
- index time: when the local sqlite index was rebuilt;
- current time: the present request handled by AstrBot.

Keeping them separate prevents an old imported diary from looking recent merely
because it was synchronized today.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


_PERIOD_HOUR = {
    "凌晨": 2,
    "深夜": 23,
    "清晨": 6,
    "早晨": 8,
    "上午": 10,
    "正午": 12,
    "午间": 12,
    "午后": 14,
    "下午": 16,
    "傍晚": 18,
    "晚上": 20,
    "夜晚": 20,
}


def timezone_or_default(name: str = "Asia/Shanghai") -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def parse_api_timestamp(value: Any) -> float:
    """Parse Memos camelCase/snake_case timestamp values into epoch seconds."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw / 1000.0 if raw > 10_000_000_000 else raw
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        raw = float(text)
        return raw / 1000.0 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def memo_source_times(memo: dict[str, Any] | None) -> tuple[float, float]:
    """Return (source_created_ts, source_updated_ts) across Memos API variants."""
    memo = memo or {}
    created = parse_api_timestamp(
        memo.get("createTime") or memo.get("create_time")
        or memo.get("displayTime") or memo.get("display_time")
    )
    updated = parse_api_timestamp(
        memo.get("updateTime") or memo.get("update_time")
        or memo.get("displayTime") or memo.get("display_time")
    )
    if not created:
        created = updated
    if not updated:
        updated = created
    return created, updated


def _date_parts(value: Any) -> tuple[int | None, int | None, int | None]:
    text = str(value or "").strip()
    if not text:
        return None, None, None
    full = re.search(r"(?<!\d)(20\d{2}|19\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", text)
    if full:
        return int(full.group(1)), int(full.group(2)), int(full.group(3))
    md = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if md:
        return None, int(md.group(1)), int(md.group(2))
    return None, None, None


def _valid_local_ts(year: int, month: int, day: int, period: str, tz: ZoneInfo) -> float:
    try:
        return datetime(year, month, day, _PERIOD_HOUR.get(period, 12), tzinfo=tz).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def normalize_memory_time(
    diary: dict[str, Any] | None,
    *,
    source_created_ts: float = 0.0,
    fallback_now_ts: float = 0.0,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Normalize old/new diary time fields without confusing source time with event time."""
    diary = diary or {}
    tz = timezone_or_default(timezone_name)
    reference_ts = source_created_ts or fallback_now_ts
    reference = datetime.fromtimestamp(reference_ts, tz) if reference_ts else datetime.now(tz)
    raw = (
        diary.get("occurred_at") or diary.get("event_date") or diary.get("date")
        or diary.get("month_day") or diary.get("date_text") or diary.get("ts_text") or ""
    )
    period = str(diary.get("time_label") or "").strip()
    if not period:
        raw_text = str(raw or "")
        period = next((p for p in _PERIOD_HOUR if p in raw_text), "")
    explicit_basis = str(diary.get("time_basis") or "").strip()
    year, month, day = _date_parts(raw)
    basis = explicit_basis
    if year and month and day:
        event_ts = _valid_local_ts(year, month, day, period, tz)
        basis = basis or "explicit"
    elif month and day and reference_ts:
        year = reference.year
        event_ts = _valid_local_ts(year, month, day, period, tz)
        # A month/day far in the future relative to source creation almost certainly
        # belongs to the previous year (important for New Year migrations).
        if event_ts and event_ts > reference_ts + 31 * 86400:
            year -= 1
            event_ts = _valid_local_ts(year, month, day, period, tz)
        basis = basis or ("inferred_from_source" if source_created_ts else "inferred_from_current")
    elif reference_ts and (
        explicit_basis == "conversation_now"
        or str(raw or "").strip() not in {"", "未注明", "未注明时间", "手动记录"}
    ):
        year, month, day = reference.year, reference.month, reference.day
        event_ts = _valid_local_ts(year, month, day, period, tz)
        basis = basis or "source_time_fallback"
    else:
        event_ts = 0.0
        year = month = day = None
        basis = "unknown" if basis in {"", "explicit_dialogue", "conversation_now"} else basis
    occurred_at = f"{year:04d}-{month:02d}-{day:02d}" if event_ts and year and month and day else ""
    display = f"{year}年{month}月{day}日" if occurred_at else "未注明时间"
    if period and occurred_at:
        display += f" · {period}"
    return {
        "occurred_at": occurred_at,
        "event_ts": float(event_ts or 0.0),
        "time_basis": basis,
        "time_label": period,
        "display": display,
    }


def relative_memory_age(event_ts: float, now_ts: float) -> str:
    if not event_ts or not now_ts:
        return "时间距离未知"
    days = int((now_ts - event_ts) // 86400)
    if days < 0:
        return f"相对当前约在 {abs(days)} 天后"
    if days == 0:
        return "发生于今天较早时段（仍不是本轮刚发生）"
    if days == 1:
        return "发生于约 1 天前"
    if days < 31:
        return f"发生于约 {days} 天前"
    if days < 365:
        return f"发生于约 {days // 30} 个月前"
    years = days // 365
    return f"发生于约 {years} 年前"


def temporal_anniversary_query(text: str) -> bool:
    """Only enable same-month/day boosting for explicit anniversary intent."""
    q = str(text or "")
    return any(k in q for k in ("纪念日", "周年", "去年今天", "那年今天", "往年今天", "每年今天"))
