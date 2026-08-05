"""
Time Enhancer - Rich temporal awareness for RP scenarios

Provides enhanced time information injection:
- Full datetime with weekday
- Lunar calendar date (Chinese)
- Season and time-of-day description
- Festival/holiday detection
- Conversation rhythm: turns today, time since last message
- Intercepts and replaces AstrBot's default datetime_system_prompt

Does NOT import any external lunar calendar library;
uses a simplified built-in calculation for the lunar date.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class TimeContext:
    """Rich temporal context"""
    # Core
    datetime_str: str      # 2026-06-20 12:48 (CST)
    weekday: str           # 星期五
    # Season & period
    season: str            # 盛夏
    time_period: str       # 午后
    # Lunar
    lunar_date: str        # 五月初六 (approximate)
    # Festival
    festival: str | None   # 端午节 / None
    # Conversation rhythm
    turns_today: int       # 今日第 N 轮
    time_since_last: str   # "3分钟前" / "首次对话"


# Weekday names
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# Season mapping (by month-day ranges, Northern Hemisphere)
_SEASONS = [
    ((2, 4), (4, 19), "初春"),
    ((4, 20), (5, 20), "暮春"),
    ((5, 21), (7, 6), "盛夏"),
    ((7, 7), (8, 22), "仲夏"),
    ((8, 23), (9, 22), "初秋"),
    ((9, 23), (11, 6), "深秋"),
    ((11, 7), (1, 5), "寒冬"),
    ((1, 6), (2, 3), "隆冬"),
]

# Time period mapping (by hour)
_TIME_PERIODS = [
    (0, 5, "深夜"),
    (5, 7, "清晨"),
    (7, 9, "早晨"),
    (9, 11, "上午"),
    (11, 13, "午间"),
    (13, 15, "午后"),
    (15, 17, "下午"),
    (17, 19, "傍晚"),
    (19, 21, "夜晚"),
    (21, 24, "深夜"),
]

# Major Chinese festivals (solar calendar approximation)
_SOLAR_FESTIVALS: dict[tuple[int, int], str] = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (4, 5): "清明节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
    (12, 31): "跨年夜",
}

# Lunar festivals keyed by (lunar_month, lunar_day) — precise via lookup table.
# Works for ANY year since we now convert solar->lunar exactly.
_LUNAR_FESTIVALS: dict[tuple[int, int], str] = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (5, 5): "端午节",
    (7, 7): "七夕",
    (7, 15): "中元节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (12, 8): "腊八节",
}

# AstrBot default system_reminder pattern to intercept
_SYSTEM_REMINDER_PATTERN = re.compile(
    r"<system_reminder>.*?</system_reminder>",
    flags=re.DOTALL,
)
_DATETIME_LINE_PATTERN = re.compile(r"Current datetime:\s*[^\n]+")


def _get_season(month: int, day: int) -> str:
    """Get season description from month/day"""
    for (m1, d1), (m2, d2), name in _SEASONS:
        if m1 <= m2:
            if (month > m1 or (month == m1 and day >= d1)) and \
               (month < m2 or (month == m2 and day <= d2)):
                return name
        else:
            # Wraps around year (e.g. Nov-Jan)
            if (month > m1 or (month == m1 and day >= d1)) or \
               (month < m2 or (month == m2 and day <= d2)):
                return name
    return "时节"


def _get_time_period(hour: int) -> str:
    """Get time-of-day description"""
    for start, end, name in _TIME_PERIODS:
        if start <= hour < end:
            return name
    return "时刻"


def _get_festival(month: int, day: int) -> str | None:
    """Check if today is a SOLAR festival (kept for backward compat).

    Lunar festivals are now handled by _get_festival_dt() with precise conversion.
    """
    if (month, day) in _SOLAR_FESTIVALS:
        return _SOLAR_FESTIVALS[(month, day)]
    return None


def _get_festival_dt(dt: datetime) -> str | None:
    """Check festival using BOTH solar date and precise lunar conversion.

    Solar festivals match on the Gregorian month/day. Lunar festivals
    (春节/端午/中秋 等) are matched after converting to the exact lunar
    date via the lookup table, so they're correct for any year.
    除夕 = last day of lunar 12th month (廿九 or 三十).
    """
    # Solar festivals first
    solar = _get_festival(dt.month, dt.day)
    if solar:
        return solar

    # Precise lunar festivals
    ly, lm, ld, is_leap = _solar_to_lunar(dt)
    if is_leap:
        return None  # leap-month days are never the festival itself
    if (lm, ld) in _LUNAR_FESTIVALS:
        return _LUNAR_FESTIVALS[(lm, ld)]

    # 除夕 = last day of the 12th lunar month (could be 29 or 30)
    if lm == 12:
        last_day = _lunar_month_days(ly, 12)
        if ld == last_day:
            return "除夕"

    return None


# Lunar calendar lookup table (1900-2039), standard algorithm
# Each hex encodes: bit 0-3=leap month(0=no leap), bit 4-15=month lengths, bit 16=leap month length
_LUNAR_INFO = [
    0x04bd8,0x04ae0,0x0a570,0x054d5,0x0d260,0x0d950,0x16554,0x056a0,0x09ad0,0x055d2,
    0x04ae0,0x0a5b6,0x0a4d0,0x0d250,0x1d255,0x0b540,0x0d6a0,0x0ada2,0x095b0,0x14977,
    0x04970,0x0a4b0,0x0b4b5,0x06a50,0x06d40,0x1ab54,0x02b60,0x09570,0x052f2,0x04970,
    0x06566,0x0d4a0,0x0ea50,0x06e95,0x05ad0,0x02b60,0x186e3,0x092e0,0x1c8d7,0x0c950,
    0x0d4a0,0x1d8a6,0x0b550,0x056a0,0x1a5b4,0x025d0,0x092d0,0x0d2b2,0x0a950,0x0b557,
    0x06ca0,0x0b550,0x15355,0x04da0,0x0a5b0,0x14573,0x052b0,0x0a9a8,0x0e950,0x06aa0,
    0x0aea6,0x0ab50,0x04b60,0x0aae4,0x0a570,0x05260,0x0f263,0x0d950,0x05b57,0x056a0,
    0x096d0,0x04dd5,0x04ad0,0x0a4d0,0x0d4d4,0x0d250,0x0d558,0x0b540,0x0b6a0,0x195a6,
    0x095b0,0x049b0,0x0a974,0x0a4b0,0x0b27a,0x06a50,0x06d40,0x0af46,0x0ab60,0x09570,
    0x04af5,0x04970,0x064b0,0x074a3,0x0ea50,0x06b58,0x055c0,0x0ab60,0x096d5,0x092e0,
    0x0c960,0x0d954,0x0d4a0,0x0da50,0x07552,0x056a0,0x0abb7,0x025d0,0x092d0,0x0cab5,
    0x0a950,0x0b4a0,0x0baa4,0x0ad50,0x055d9,0x04ba0,0x0a5b0,0x15176,0x052b0,0x0a930,
    0x07954,0x06aa0,0x0ad50,0x05b52,0x04b60,0x0a6e6,0x0a4e0,0x0d260,0x0ea65,0x0d530,
    0x05aa0,0x076a3,0x096d0,0x04afb,0x04ad0,0x0a4d0,0x1d0b6,0x0d250,0x0d520,0x0dd45,
]

def _lunar_leap_month(year: int) -> int:
    """Return leap month (1-12) or 0 if no leap month."""
    return _LUNAR_INFO[year - 1900] & 0xf

def _lunar_leap_days(year: int) -> int:
    """Days in leap month (29 or 30), or 0 if no leap month."""
    lm = _lunar_leap_month(year)
    if lm == 0:
        return 0
    return 30 if (_LUNAR_INFO[year - 1900] & 0x10000) else 29

def _lunar_month_days(year: int, month: int) -> int:
    """Days in a normal lunar month (29 or 30)."""
    return 30 if (_LUNAR_INFO[year - 1900] & (0x10000 >> month)) else 29

def _lunar_year_days(year: int) -> int:
    """Total days in a lunar year (including leap month if any)."""
    days = sum(_lunar_month_days(year, m) for m in range(1, 13))
    return days + _lunar_leap_days(year)

def _solar_to_lunar(dt: datetime) -> tuple[int, int, int, bool]:
    """Convert solar date to lunar (year, month, day, is_leap_month).

    Uses standard LUNAR_INFO table (1900-2039). Returns (year, month, day, is_leap).
    Verified against the `lunardate`/`zhdate` libraries including leap-month years.
    """
    from datetime import date
    if dt.year < 1900 or dt.year > 2039:
        # Fallback for out-of-range: return solar values (rare)
        return (dt.year, dt.month, dt.day, False)

    offset = (date(dt.year, dt.month, dt.day) - date(1900, 1, 31)).days

    i_year = 1900
    days_of_year = 0
    while i_year < 2100 and offset > 0:
        days_of_year = _lunar_year_days(i_year)
        offset -= days_of_year
        i_year += 1
    if offset < 0:
        offset += days_of_year
        i_year -= 1

    leap_m = _lunar_leap_month(i_year)
    leap = False
    i_month = 1
    days_of_month = 0
    while i_month < 13 and offset > 0:
        if leap_m > 0 and i_month == (leap_m + 1) and not leap:
            i_month -= 1
            leap = True
            days_of_month = _lunar_leap_days(i_year)
        else:
            days_of_month = _lunar_month_days(i_year, i_month)
        offset -= days_of_month
        if leap and i_month == (leap_m + 1):
            leap = False
        i_month += 1

    if offset == 0 and leap_m > 0 and i_month == leap_m + 1:
        if leap:
            leap = False
        else:
            leap = True
            i_month -= 1

    if offset < 0:
        offset += days_of_month
        i_month -= 1

    return (i_year, i_month, offset + 1, leap)

def _format_lunar_date(year: int, month: int, day: int, is_leap: bool) -> str:
    """Format lunar date as Chinese string."""
    month_names = [
        "正月", "二月", "三月", "四月", "五月", "六月",
        "七月", "八月", "九月", "十月", "冬月", "腊月"
    ]
    day_prefixes = ["初", "初", "初", "初", "初", "初", "初", "初", "初", "初",
                    "十", "十", "十", "十", "十", "十", "十", "十", "十", "十",
                    "廿", "廿", "廿", "廿", "廿", "廿", "廿", "廿", "廿", "三十"]
    day_nums = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
                "一", "二", "三", "四", "五", "六", "七", "八", "九", "二十",
                "一", "二", "三", "四", "五", "六", "七", "八", "九", ""]

    m_name = month_names[month - 1] if 1 <= month <= 12 else "正月"
    if is_leap:
        m_name = "闰" + m_name

    if day < 1:
        day = 1
    if day > 30:
        day = 30
    d_str = day_prefixes[day - 1] + day_nums[day - 1]

    return m_name + d_str

def _approx_lunar_date(dt: datetime) -> str:
    """Precise lunar date via lookup table (1900-2039)."""
    y, m, d, is_leap = _solar_to_lunar(dt)
    return _format_lunar_date(y, m, d, is_leap)


def _format_time_since(seconds: float) -> str:
    """Format seconds into human-readable Chinese duration"""
    if seconds < 60:
        return "刚刚"
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins}分钟前"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours}小时前"
    else:
        days = int(seconds / 86400)
        return f"{days}天前"


class TimeEnhancer:
    """Manages rich temporal context and conversation rhythm tracking"""

    def __init__(self, timezone_str: str = "Asia/Shanghai"):
        self.timezone_str = timezone_str
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")

        # Per-session tracking
        self._session_last_time: dict[str, float] = {}
        self._session_today_turns: dict[str, int] = {}
        self._session_today_date: dict[str, str] = {}

    def get_time_context(
        self,
        session_id: str,
        now: datetime | None = None,
    ) -> TimeContext:
        """Build time context from one request-scoped instant."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        now = current.astimezone(self.tz)
        now_ts = current.timestamp()
        today_str = now.strftime("%Y-%m-%d")

        # Update session tracking
        last_time = self._session_last_time.get(session_id, 0)
        time_since = max(0.0, now_ts - last_time) if last_time > 0 else 0
        time_since_str = _format_time_since(time_since) if last_time > 0 else "首次对话"

        # Reset daily counter if new day
        if self._session_today_date.get(session_id) != today_str:
            self._session_today_turns[session_id] = 0
            self._session_today_date[session_id] = today_str

        self._session_today_turns[session_id] = \
            self._session_today_turns.get(session_id, 0) + 1
        self._session_last_time[session_id] = now_ts

        turns_today = self._session_today_turns[session_id]

        return TimeContext(
            datetime_str=now.strftime("%Y-%m-%d %H:%M"),
            weekday=_WEEKDAYS[now.weekday()],
            season=_get_season(now.month, now.day),
            time_period=_get_time_period(now.hour),
            lunar_date=_approx_lunar_date(now),
            festival=_get_festival_dt(now),
            turns_today=turns_today,
            time_since_last=time_since_str,
        )

    def format_injection(self, ctx: TimeContext) -> str:
        """Format time context into injection text"""
        lines = [
            '<CurrentTimeContext priority="critical" role="only_current_now">',
            f"本轮唯一当前时间: {ctx.datetime_str} {ctx.weekday}",
            f"时令: {ctx.season} · {ctx.time_period}",
        ]
        if ctx.lunar_date:
            lines.append(f"农历: {ctx.lunar_date}")
        if ctx.festival:
            lines.append(f"节日: {ctx.festival}")
        rhythm_tail = "本会话首次对话" if ctx.time_since_last == "首次对话" else f"距上次对话{ctx.time_since_last}"
        lines.append(f"对话节奏: 今日第{ctx.turns_today}轮 · {rhythm_tail}")
        lines.extend([
            "时间边界: 本块是本轮唯一现实现在；HistoricalMemory.occurred_at 与 Memos 创建/同步时间均为过去的来源时间。",
            "叙事边界: 不把旧日记当成刚刚发生；用户明确设定剧情时间时按用户设定叙事，但不要改写现实时间与身体节律。",
            "</CurrentTimeContext>",
        ])
        return "\n".join(lines)

    @staticmethod
    def strip_default_system_reminder(parts: list) -> list:
        """Remove AstrBot's default <system_reminder> from extra_user_content_parts.

        Searches for TextPart containing <system_reminder>...</system_reminder>
        and strips the Current datetime line from it (or removes entirely if
        that's the only content).

        Args:
            parts: list of ContentPart (from req.extra_user_content_parts)

        Returns:
            Modified list with datetime reminder stripped
        """
        kept = []
        for part in parts:
            text = getattr(part, "text", None)
            if text is None:
                kept.append(part)
                continue

            if "<system_reminder>" not in text:
                kept.append(part)
                continue

            cleaned = TimeEnhancer.strip_default_datetime_text(text)
            if cleaned.strip():
                try:
                    part.text = cleaned
                    kept.append(part)
                except Exception:
                    kept.append(part)
        return kept

    @staticmethod
    def strip_default_datetime_text(text: str) -> str:
        """Remove AstrBot's default Current datetime reminder from a text block."""
        text = text or ""

        def clean_reminder(match: re.Match) -> str:
            block = match.group(0)
            inner = block.replace("<system_reminder>", "").replace("</system_reminder>", "")
            inner = _DATETIME_LINE_PATTERN.sub("", inner)
            inner = "\n".join(line for line in inner.splitlines() if line.strip())
            if not inner.strip():
                return ""
            return "<system_reminder>" + inner.strip() + "</system_reminder>"

        text = _SYSTEM_REMINDER_PATTERN.sub(clean_reminder, text)
        text = _DATETIME_LINE_PATTERN.sub("", text)
        return "\n".join(line for line in text.splitlines() if line.strip()).strip()

    @staticmethod
    def strip_default_datetime_system_prompt(system_prompt: str) -> str:
        """Remove default datetime reminders if a provider puts them in system_prompt."""
        return TimeEnhancer.strip_default_datetime_text(system_prompt or "")
