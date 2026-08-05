from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


PHASE_LABELS = {
    "menstrual": "经期",
    "follicular": "恢复期",
    "ovulatory": "活跃期",
    "luteal": "缓降期",
}

SIGNAL_LABELS = {
    "energy": "身体精力",
    "discomfort": "身体不适",
    "sensitivity": "感官敏感",
    "social_energy": "社交余量",
    "comfort_need": "安稳偏好",
    "closeness": "亲近感知",
}

TIME_BAND_LABELS = {
    "morning": "早晨",
    "afternoon": "午后",
    "evening": "傍晚",
    "deep_night": "深夜",
}

_NEUTRAL = {
    "energy": 0.62,
    "discomfort": 0.10,
    "sensitivity": 0.40,
    "social_energy": 0.55,
    "comfort_need": 0.42,
    "closeness": 0.38,
}


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _stable_noise(seed_text: str, key: str) -> float:
    digest = hashlib.sha256(f"{seed_text}|{key}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)
    return value * 2.0 - 1.0


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3.0 - 2.0 * value)


def _local_datetime(now: datetime | None, time_zone: str) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        return current.astimezone(ZoneInfo(time_zone))
    except Exception:
        return current.astimezone()


def _phase_baseline(
    phase: str,
    progress: float,
) -> dict[str, float]:
    p = _clamp(progress)
    if phase == "menstrual":
        return {
            "energy": 0.38 + 0.16 * p,
            "discomfort": 0.72 - 0.30 * p,
            "sensitivity": 0.62 - 0.10 * p,
            "social_energy": 0.43 + 0.08 * p,
            "comfort_need": 0.72 - 0.08 * p,
            "closeness": 0.31 + 0.04 * p,
        }
    if phase == "follicular":
        return {
            "energy": 0.58 + 0.23 * p,
            "discomfort": 0.14 - 0.04 * p,
            "sensitivity": 0.41 - 0.05 * p,
            "social_energy": 0.51 + 0.17 * p,
            "comfort_need": 0.43 - 0.08 * p,
            "closeness": 0.38 + 0.10 * p,
        }
    if phase == "ovulatory":
        centered = abs(p - 0.5) * 2.0
        return {
            "energy": 0.82 - 0.04 * centered,
            "discomfort": 0.10 + 0.04 * (1.0 - centered),
            "sensitivity": 0.41 + 0.03 * (1.0 - centered),
            "social_energy": 0.72 - 0.03 * centered,
            "comfort_need": 0.35,
            "closeness": 0.56 + 0.04 * (1.0 - centered),
        }
    late = p**1.65
    return {
        "energy": 0.72 - 0.25 * late,
        "discomfort": 0.12 + 0.35 * (p**2.0),
        "sensitivity": 0.40 + 0.27 * late,
        "social_energy": 0.58 - 0.15 * late,
        "comfort_need": 0.43 + 0.24 * late,
        "closeness": 0.44 - 0.04 * p,
    }


def _phase_geometry(settings: dict[str, Any]) -> tuple[int, int, int, int, int]:
    cycle_length = max(21, min(45, int(settings.get("body_cycle_length") or 28)))
    period_length = max(2, min(min(10, cycle_length - 6), int(settings.get("body_period_length") or 5)))
    ovulation_day = max(
        period_length + 2,
        min(cycle_length - 2, int(settings.get("body_ovulation_day") or 14)),
    )
    window = max(1, min(7, int(settings.get("body_ovulation_window") or 3)))
    ovulation_start = ovulation_day - (window - 1) // 2
    ovulation_start = max(period_length + 1, min(cycle_length - window, ovulation_start))
    ovulation_end = ovulation_start + window - 1
    return cycle_length, period_length, ovulation_day, ovulation_start, ovulation_end


def _phase_position(
    cycle_day: int,
    cycle_length: int,
    period_length: int,
    ovulation_start: int,
    ovulation_end: int,
) -> tuple[str, int, int]:
    if cycle_day <= period_length:
        return "menstrual", cycle_day, period_length
    if cycle_day < ovulation_start:
        duration = max(1, ovulation_start - period_length - 1)
        return "follicular", cycle_day - period_length, duration
    if cycle_day <= ovulation_end:
        return "ovulatory", cycle_day - ovulation_start + 1, ovulation_end - ovulation_start + 1
    duration = max(1, cycle_length - ovulation_end)
    return "luteal", cycle_day - ovulation_end, duration


def _baseline_for_cycle_day(
    cycle_day: int, cycle_length: int, period_length: int,
    ovulation_start: int, ovulation_end: int,
) -> tuple[str, int, int, float, dict[str, float]]:
    phase, phase_day, phase_duration = _phase_position(
        cycle_day, cycle_length, period_length, ovulation_start, ovulation_end,
    )
    progress = 0.5 if phase_duration <= 1 else (phase_day - 1) / (phase_duration - 1)
    return phase, phase_day, phase_duration, progress, _phase_baseline(phase, progress)


def _time_band(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "deep_night"


def _time_signal_shifts(phase: str, band: str) -> dict[str, float]:
    generic = {
        "morning": {"energy": -0.01, "sensitivity": 0.01},
        "afternoon": {"energy": -0.02},
        "evening": {"energy": -0.03, "comfort_need": 0.02},
        "deep_night": {
            "energy": -0.09,
            "sensitivity": 0.03,
            "social_energy": -0.03,
            "comfort_need": 0.04,
        },
    }
    phase_specific = {
        ("menstrual", "morning"): {"energy": -0.03, "discomfort": 0.06},
        ("menstrual", "afternoon"): {"energy": -0.02, "discomfort": -0.03},
        ("menstrual", "evening"): {"comfort_need": 0.04},
        ("menstrual", "deep_night"): {"discomfort": 0.02, "sensitivity": 0.04},
        ("follicular", "morning"): {"energy": 0.03},
        ("follicular", "afternoon"): {"energy": 0.02, "social_energy": 0.02},
        ("follicular", "deep_night"): {"energy": -0.02},
        ("ovulatory", "morning"): {"energy": 0.02},
        ("ovulatory", "afternoon"): {"social_energy": 0.03},
        ("ovulatory", "evening"): {"social_energy": 0.01, "closeness": 0.02},
        ("ovulatory", "deep_night"): {"energy": -0.02, "sensitivity": 0.02},
        ("luteal", "morning"): {"energy": -0.02},
        ("luteal", "afternoon"): {"energy": -0.03},
        ("luteal", "evening"): {"comfort_need": 0.04},
        ("luteal", "deep_night"): {
            "energy": -0.03,
            "sensitivity": 0.04,
            "comfort_need": 0.04,
        },
    }
    merged = dict(generic.get(band, {}))
    for key, value in phase_specific.get((phase, band), {}).items():
        merged[key] = merged.get(key, 0.0) + value
    return merged


def _blended_time_signal_shifts(phase: str, local_now: datetime) -> dict[str, float]:
    band = _time_band(local_now.hour)
    boundaries = {
        "morning": (12 * 60, "afternoon"),
        "afternoon": (18 * 60, "evening"),
        "evening": (23 * 60, "deep_night"),
        "deep_night": (5 * 60 if local_now.hour < 5 else 29 * 60, "morning"),
    }
    minute = local_now.hour * 60 + local_now.minute + local_now.second / 60.0
    boundary, next_band = boundaries[band]
    remaining = boundary - minute
    current = _time_signal_shifts(phase, band)
    if remaining >= 60:
        return current
    blend = _smoothstep(1.0 - max(0.0, remaining) / 60.0)
    upcoming = _time_signal_shifts(phase, next_band)
    return {
        key: current.get(key, 0.0) + (upcoming.get(key, 0.0) - current.get(key, 0.0)) * blend
        for key in set(current) | set(upcoming)
    }


def _phase_experience(phase: str, progress: float) -> str:
    position = "early" if progress < 0.34 else ("middle" if progress < 0.67 else "late")
    descriptions = {
        "menstrual": {
            "early": "身体处在较低负荷，腹部或腰背更容易有沉重、牵扯感，启动速度和持续耐力都偏低。",
            "middle": "身体仍有可感的不适和易倦，动作、注意力与耐心更适合留出一点缓冲。",
            "late": "先前的沉重感正在退去，体力开始恢复，但持续消耗后仍可能比平时更早觉得累。",
        },
        "follicular": {
            "early": "身体正从低负荷中恢复，呼吸、动作和注意力逐渐变得轻松连贯。",
            "middle": "体力储备与专注稳定度正在上升，处理事情时更容易保持清晰和连续。",
            "late": "身体整体轻快，耐力和行动流畅度较好，对外界变化也更有余量。",
        },
        "ovulatory": {
            "early": "身体进入较轻快的状态，行动、交流和感官接收都更流畅，但仍服从当前情境。",
            "middle": "体力与社交余量较充足，身体距离和细小触感更容易进入注意范围。",
            "late": "轻快感仍然存在，但强度开始缓慢回落，不需要刻意维持兴奋或主动。",
        },
        "luteal": {
            "early": "身体总体仍然平稳，只是长时间活动后的恢复速度开始略慢。",
            "middle": "持续消耗后更容易出现疲乏、注意力松动或对刺激更敏锐的感觉。",
            "late": "身体负担与感官敏感逐渐上升，更适合保留体力、降低刺激并给反应留出余地。",
        },
    }
    return descriptions[phase][position]


def _time_experience(phase: str, band: str) -> str:
    descriptions = {
        "menstrual": {
            "morning": "刚醒后的身体沉重和局部不适更容易被察觉，活动一阵后才会慢慢松开。",
            "afternoon": "身体已比早晨适应一些，但连续活动后困倦和体力回落会更早出现。",
            "evening": "身体开始偏向安静、温暖和少折腾的节奏，休息感比额外刺激更舒服。",
            "deep_night": "夜深后的疲劳会放大身体负担和细小刺激，表达与动作可以自然收慢。",
        },
        "follicular": {
            "morning": "醒后恢复较快，身体与注意力能较自然地进入当天节奏。",
            "afternoon": "体力和专注通常较连贯，处理事情或交流时不容易很快耗尽。",
            "evening": "白天的清晰感仍有余量，但身体开始自然收束，不必刻意保持高活跃。",
            "deep_night": "夜深后轻快感会让位于睡意和安静需求，身体不需要继续维持白天强度。",
        },
        "ovulatory": {
            "morning": "起身时身体较轻快，启动行动和组织表达都不太费力。",
            "afternoon": "体力与互动余量处在较好区间，更能承受连续活动，但不等于必须热情。",
            "evening": "身体仍保留一些活力，对声音、触碰和彼此距离的感知会稍微鲜明。",
            "deep_night": "即使白天余量较足，夜深后身体仍会回落，应自然接纳疲劳而不是强撑活跃。",
        },
        "luteal": {
            "morning": "刚醒时启动速度可能略慢，睡眠不足留下的沉重感也更容易被察觉。",
            "afternoon": "经过半天消耗后，体力下降和注意力分散会比平稳时更明显一些。",
            "evening": "身体更偏向熟悉、低刺激和能放松下来的环境，节奏可以逐渐收束。",
            "deep_night": "夜深后疲劳与感官负担更容易累积，适合更短、更安静、更有停顿的反应。",
        },
    }
    return descriptions[phase][band]


def _render_tendencies(
    signals: dict[str, float],
    include_closeness: bool,
    limit: int = 3,
) -> list[str]:
    candidates: list[tuple[float, str]] = []
    energy = signals["energy"]
    if energy <= 0.43:
        candidates.append((
            0.62 - energy,
            "身体精力偏低，动作与表达节奏可以自然放慢，但不必反复强调疲惫。",
        ))
    elif energy >= 0.75:
        candidates.append((
            energy - 0.62,
            "身体状态较轻快，行动意愿和表达流畅度可以略高。",
        ))

    discomfort = signals["discomfort"]
    if discomfort >= 0.62:
        candidates.append((
            discomfort - 0.10,
            "身体有较明显但可承受的不适，注意力偶尔会被牵走，仍能正常交流。",
        ))
    elif discomfort >= 0.36:
        candidates.append((
            discomfort - 0.10,
            "身体有轻微不适，姿态和耐心可以稍微收敛，不需要主动解释原因。",
        ))

    sensitivity = signals["sensitivity"]
    if sensitivity >= 0.60:
        candidates.append((
            sensitivity - 0.40,
            "身体敏感度偏高，对触碰、声音和语气的细小变化更容易察觉；这不等于自动生气。",
        ))

    social = signals["social_energy"]
    if social <= 0.44:
        candidates.append((
            0.55 - social,
            "社交余量略低，回复可以更简洁安静，但不代表拒绝对方。",
        ))

    comfort = signals["comfort_need"]
    if comfort >= 0.62:
        candidates.append((
            comfort - 0.42,
            "身体更偏好温暖、安稳和少折腾的相处方式，但不应无缘无故索取安慰。",
        ))

    closeness = signals["closeness"]
    if include_closeness and closeness >= 0.55:
        candidates.append((
            closeness - 0.38,
            "对身体距离与亲近更有感知；是否靠近仍完全服从人物边界、关系阶段和当前情境。",
        ))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return []
    return [text for _, text in candidates[:max(1, min(4, int(limit)))]]


def _soften_experience(text: str, strength: float) -> str:
    if strength < 0.20:
        return f"只有很轻的倾向：{text}"
    if strength < 0.45:
        return f"这是偏轻的身体底色：{text}"
    return text


def calculate_body_state(
    settings: dict[str, Any],
    now: datetime | None = None,
    character_key: str = "",
) -> dict[str, Any]:
    enabled = bool(settings.get("body_rhythm_enable", True))
    if not enabled:
        return {"available": False, "reason": "disabled", "signals": {}, "tendencies": []}

    anchor_text = str(settings.get("body_anchor_date") or "").strip()
    if not anchor_text:
        return {"available": False, "reason": "anchor_missing", "signals": {}, "tendencies": []}
    try:
        anchor = date.fromisoformat(anchor_text)
    except ValueError:
        return {"available": False, "reason": "anchor_invalid", "signals": {}, "tendencies": []}

    local_now = _local_datetime(now, str(settings.get("time_zone") or "Asia/Shanghai"))
    cycle_length, period_length, ovulation_day, ovulation_start, ovulation_end = _phase_geometry(settings)
    cycle_day = ((local_now.date() - anchor).days % cycle_length) + 1
    phase, phase_day, phase_duration, progress, baseline_today = _baseline_for_cycle_day(
        cycle_day, cycle_length, period_length, ovulation_start, ovulation_end,
    )
    next_cycle_day = 1 if cycle_day >= cycle_length else cycle_day + 1
    next_phase, _, _, _, baseline_tomorrow = _baseline_for_cycle_day(
        next_cycle_day, cycle_length, period_length, ovulation_start, ovulation_end,
    )
    seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    day_blend = _smoothstep(seconds / 86400.0)
    baseline = {
        key: value + (baseline_tomorrow[key] - value) * day_blend
        for key, value in baseline_today.items()
    }

    strength = _clamp(settings.get("body_effect_strength", 0.72))
    variability = _clamp(settings.get("body_daily_variation", 0.22))
    seed_today = f"{character_key}|{anchor_text}|{local_now.date().isoformat()}|{cycle_day}"
    tomorrow = local_now.date() + timedelta(days=1)
    seed_tomorrow = f"{character_key}|{anchor_text}|{tomorrow.isoformat()}|{next_cycle_day}"
    scales = {
        "energy": _clamp(settings.get("body_energy_scale", 1.0), 0.0, 1.5),
        "discomfort": _clamp(settings.get("body_discomfort_scale", 1.0), 0.0, 1.5),
        "sensitivity": _clamp(settings.get("body_sensitivity_scale", 1.0), 0.0, 1.5),
        "social_energy": 1.0,
        "comfort_need": 1.0,
        "closeness": _clamp(settings.get("body_closeness_scale", 0.65), 0.0, 1.5),
    }
    signals: dict[str, float] = {}
    for key, base in baseline.items():
        noise_today = _stable_noise(seed_today, key)
        noise_tomorrow = _stable_noise(seed_tomorrow, key)
        smooth_noise = noise_today + (noise_tomorrow - noise_today) * day_blend
        variation = smooth_noise * 0.16 * variability
        departure = (base + variation) - _NEUTRAL[key]
        signals[key] = round(_clamp(_NEUTRAL[key] + departure * strength * scales[key]), 3)

    band = _time_band(local_now.hour)
    time_enabled = bool(settings.get("body_time_modulation", True))
    if time_enabled:
        shifts_today = _blended_time_signal_shifts(phase, local_now)
        shifts_tomorrow = _blended_time_signal_shifts(next_phase, local_now)
        blended_shifts = {
            key: shifts_today.get(key, 0.0)
            + (shifts_tomorrow.get(key, 0.0) - shifts_today.get(key, 0.0)) * day_blend
            for key in set(shifts_today) | set(shifts_tomorrow)
        }
        for key, shift in blended_shifts.items():
            signals[key] = round(_clamp(signals[key] + shift * strength), 3)

    tendencies = _render_tendencies(
        signals,
        bool(settings.get("body_closeness_influence", True)),
        limit=4,
    )
    expression_mode = str(settings.get("body_expression_mode") or "balanced").strip()
    if expression_mode not in {"significant", "balanced", "immersive"}:
        expression_mode = "balanced"
    phase_experience = (
        _soften_experience(_phase_experience(phase, progress), strength)
        if strength >= 0.05
        else ""
    )
    time_experience = (
        _soften_experience(_time_experience(phase, band), strength)
        if time_enabled and strength >= 0.05
        else ""
    )
    state = {
        "available": True,
        "reason": "ok",
        "date": local_now.date().isoformat(),
        "phase": phase,
        "phaseLabel": PHASE_LABELS[phase],
        "cycleDay": cycle_day,
        "phaseDay": phase_day,
        "phaseDuration": phase_duration,
        "cycleLength": cycle_length,
        "periodLength": period_length,
        "ovulationDay": ovulation_day,
        "daysToNextCycle": cycle_length - cycle_day + 1,
        "progress": round(_clamp(progress), 3),
        "signals": signals,
        "signalLabels": SIGNAL_LABELS,
        "tendencies": tendencies,
        "phaseExperience": phase_experience,
        "timeBand": band,
        "timeLabel": TIME_BAND_LABELS[band],
        "timeExperience": time_experience,
        "expressionMode": expression_mode,
        "effectStrength": round(strength, 3),
        "dailyVariation": round(variability, 3),
        "transitionProgress": round(day_blend, 3),
        "promptPhaseHidden": True,
    }
    context_lines = render_body_context(state)
    state["contextLines"] = context_lines
    state["injectionPreview"] = "\n".join(context_lines)
    return state


def render_body_context(body_state: dict[str, Any]) -> list[str]:
    if not isinstance(body_state, dict) or not body_state.get("available"):
        return []
    mode = str(body_state.get("expressionMode") or "balanced")
    tendencies = [
        " ".join(str(item or "").split())
        for item in list(body_state.get("tendencies") or [])
        if str(item or "").strip()
    ]
    phase_experience = " ".join(str(body_state.get("phaseExperience") or "").split())
    time_experience = " ".join(str(body_state.get("timeExperience") or "").split())
    if mode == "significant":
        content = [f"- 显著身体信号：{item}" for item in tendencies[:3]]
    else:
        content = []
        if phase_experience:
            content.append(f"- 主体感受：{phase_experience}")
        if time_experience:
            content.append(f"- 此刻的日内修正：{time_experience}")
        tendency_limit = 3 if mode == "immersive" else 2
        content.extend(f"- 显著身体信号：{item}" for item in tendencies[:tendency_limit])
    if not content:
        return []
    return [
        "当前身体底色（仅代表此刻的主体体验，不是历史事件，也不写入长期人格或事实记忆）：",
        *content,
        "把这些感受内化为动作幅度、注意力、耐力、停顿和措辞的细微差别；只有当前场景自然涉及身体时才可以含蓄表现，不要逐条复述。",
        "身体信号不能自动制造生气、撒娇、拒绝、依恋、欲望或关系结论，也不要主动报告周期阶段、日期或数值。",
    ]
