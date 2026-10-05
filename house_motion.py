"""Deterministic scene and motion selection for the optional courtyard UI."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def _profile(
    profile_id: str,
    label: str,
    primitive: str,
    duration: float,
    amplitude: float,
    *,
    drives: tuple[str, ...] = (),
    periods: tuple[str, ...] = (),
    fatigue: tuple[str, ...] = (),
    artifacts: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": profile_id,
        "label": label,
        "primitive": primitive,
        "duration": duration,
        "amplitude": amplitude,
        "drives": drives,
        "periods": periods,
        "fatigue": fatigue,
        "artifacts": artifacts,
    }


# Five semantic motion profiles per art state. They share a small set of safe
# whole-sprite primitives, but differ in timing, amplitude and trigger weights.
MOTION_PROFILES: dict[str, tuple[dict[str, Any], ...]] = {
    "fan": (
        _profile("fan_quiet_breath", "安静呼吸", "breathe", 5.4, 0.55, drives=("reflection", "monitor"), periods=("day", "dusk")),
        _profile("fan_weight_shift", "重心轻移", "sway", 4.8, 0.72, drives=("social", "boredom"), periods=("day",)),
        _profile("fan_soft_turn", "轻缓侧身", "turn", 4.2, 0.62, drives=("monitor", "curiosity")),
        _profile("fan_listening_nod", "闻声点头", "nod", 3.6, 0.48, drives=("social", "share"), artifacts=("whisper",)),
        _profile("fan_long_gaze", "短暂凝望", "settle", 5.8, 0.38, drives=("possess", "grieve"), periods=("dusk", "night")),
    ),
    "letter": (
        _profile("letter_read_pause", "读信停顿", "settle", 5.2, 0.42, drives=("share", "reflection"), artifacts=("letter", "unsent_note")),
        _profile("letter_small_startle", "轻微惊起", "startle", 3.2, 0.55, drives=("monitor", "curiosity")),
        _profile("letter_hesitate_back", "迟疑后退", "recoil", 4.0, 0.58, drives=("grieve", "anger"), fatigue=("mid", "high")),
        _profile("letter_return_near", "回神靠近", "lean", 4.3, 0.52, drives=("social", "possess", "share")),
        _profile("letter_settle", "慢慢平复", "breathe", 5.7, 0.45, drives=("reflection",), periods=("dusk", "night")),
    ),
    "tea": (
        _profile("tea_seated_breath", "坐姿呼吸", "breathe", 5.9, 0.48, drives=("reflection", "monitor")),
        _profile("tea_body_settle", "身体回稳", "settle", 5.0, 0.42, fatigue=("mid", "high")),
        _profile("tea_listening_lean", "倾听前倾", "lean", 4.2, 0.46, drives=("social", "monitor"), artifacts=("unfinished_thread",)),
        _profile("tea_soft_nod", "轻轻点头", "nod", 3.8, 0.42, drives=("share", "duty")),
        _profile("tea_wait", "安静等待", "stillness", 6.2, 0.28, drives=("possess", "monitor"), periods=("dusk", "night")),
    ),
    "desk": (
        _profile("desk_writing", "落笔节奏", "write", 4.4, 0.58, drives=("duty", "share"), artifacts=("letter", "reflection")),
        _profile("desk_thought_pause", "思考停顿", "settle", 5.0, 0.36, drives=("reflection", "curiosity")),
        _profile("desk_lower_return", "低头回稳", "dip", 4.3, 0.44, drives=("duty",)),
        _profile("desk_attentive_lift", "闻声抬起", "lift", 3.8, 0.54, drives=("social", "monitor")),
        _profile("desk_refocus", "重新专注", "breathe", 5.6, 0.35, drives=("duty", "reflection"), periods=("day", "night")),
    ),
    "window": (
        _profile("window_slow_sink", "缓慢下沉", "dip", 5.8, 0.46, drives=("grieve", "reflection"), fatigue=("high",)),
        _profile("window_sleepy_return", "困倦回正", "lift", 5.2, 0.40, fatigue=("mid", "high"), periods=("night", "dawn")),
        _profile("window_distant_turn", "望向远处", "turn", 5.5, 0.42, drives=("monitor", "grieve")),
        _profile("window_heard_lift", "闻声抬眼", "nod", 4.0, 0.38, drives=("social", "possess")),
        _profile("window_long_still", "长时间静止", "stillness", 6.5, 0.20, drives=("reflection",), fatigue=("high",)),
    ),
    "chest": (
        _profile("chest_inspect_lean", "探身查看", "lean", 4.5, 0.48, drives=("curiosity",), artifacts=("reflection",)),
        _profile("chest_careful_pause", "谨慎停顿", "settle", 5.2, 0.34, drives=("duty", "reflection")),
        _profile("chest_ask_turn", "回头询问", "turn", 4.1, 0.50, drives=("social", "monitor")),
        _profile("chest_small_recoil", "轻微退让", "recoil", 4.0, 0.44, drives=("anger", "grieve")),
        _profile("chest_return_near", "重新靠近", "lean", 4.8, 0.38, drives=("possess", "curiosity")),
    ),
    "playful": (
        _profile("playful_light_bounce", "轻快起伏", "greet", 3.6, 0.62, drives=("boredom", "social"), periods=("day",)),
        _profile("playful_side_tilt", "俏皮偏身", "sway", 4.0, 0.64, drives=("curiosity", "share")),
        _profile("playful_approach", "主动靠近", "lean", 3.8, 0.58, drives=("possess", "crave")),
        _profile("playful_greeting", "招呼回应", "greet", 3.5, 0.52, drives=("social", "monitor")),
        _profile("playful_settle", "笑后回稳", "breathe", 4.8, 0.40, drives=("share",), fatigue=("mid",)),
    ),
    "reading": (
        _profile("reading_breath", "阅读呼吸", "breathe", 5.8, 0.34, drives=("reflection", "curiosity")),
        _profile("reading_thought_pause", "思绪停顿", "settle", 5.3, 0.30, drives=("reflection",), periods=("night",)),
        _profile("reading_slow_lift", "缓慢抬头", "lift", 4.4, 0.42, drives=("monitor", "social")),
        _profile("reading_listen_turn", "侧身倾听", "turn", 4.2, 0.44, drives=("share", "possess")),
        _profile("reading_return_quiet", "回到安静", "dip", 5.1, 0.32, drives=("duty", "reflection")),
    ),
    "lantern": (
        _profile("lantern_walk_weight", "行走重心", "sway", 4.8, 0.62, drives=("monitor", "duty"), periods=("dusk", "night")),
        _profile("lantern_halt", "提灯停步", "settle", 4.3, 0.42, drives=("curiosity", "monitor")),
        _profile("lantern_heard_turn", "循声回眸", "turn", 4.0, 0.58, drives=("social", "possess")),
        _profile("lantern_careful_near", "谨慎靠近", "lean", 4.4, 0.46, drives=("monitor", "crave")),
        _profile("lantern_stand", "灯下静立", "breathe", 5.8, 0.34, drives=("reflection", "grieve")),
    ),
    "wistful": (
        _profile("wistful_long_still", "长久静止", "stillness", 6.4, 0.18, drives=("grieve", "reflection")),
        _profile("wistful_slow_turn", "缓慢回眸", "turn", 5.0, 0.42, drives=("monitor", "possess")),
        _profile("wistful_small_retreat", "轻微退身", "recoil", 4.8, 0.36, drives=("anger", "grieve")),
        _profile("wistful_release", "呼吸放松", "breathe", 6.0, 0.38, drives=("reflection",), fatigue=("mid", "high")),
        _profile("wistful_look_away", "重新望向院中", "sway", 5.4, 0.32, drives=("monitor",), periods=("dusk", "night")),
    ),
    "flowers": (
        _profile("flowers_kneel_breath", "跪坐呼吸", "breathe", 5.8, 0.38, drives=("reflection",), periods=("dawn", "day")),
        _profile("flowers_gentle_lean", "轻缓探身", "lean", 4.7, 0.38, drives=("curiosity",)),
        _profile("flowers_bright_lift", "欣然抬起", "lift", 4.0, 0.44, drives=("share", "social")),
        _profile("flowers_soft_nod", "柔和点头", "nod", 3.9, 0.36, drives=("monitor", "possess")),
        _profile("flowers_wait", "低头静候", "dip", 5.2, 0.32, drives=("reflection", "grieve")),
    ),
    "steps": (
        _profile("steps_seated_shift", "坐姿轻移", "sway", 4.8, 0.44, drives=("boredom", "social")),
        _profile("steps_invite_turn", "邀请侧身", "turn", 4.1, 0.52, drives=("social", "share"), artifacts=("unfinished_thread",)),
        _profile("steps_laugh_bounce", "短促笑意", "greet", 3.5, 0.56, drives=("boredom", "curiosity")),
        _profile("steps_reply_nod", "回应点头", "nod", 3.8, 0.40, drives=("monitor", "duty")),
        _profile("steps_relax", "舒展回稳", "breathe", 5.4, 0.36, drives=("reflection",), fatigue=("mid",)),
    ),
    "yawn": (
        _profile("yawn_sleepy_sink", "困倦下沉", "dip", 5.4, 0.52, fatigue=("high",), periods=("night", "dawn")),
        _profile("yawn_wake_start", "突然回神", "startle", 3.4, 0.50, drives=("monitor", "social")),
        _profile("yawn_slow_nod", "迟缓点头", "nod", 4.5, 0.32, drives=("possess", "share"), fatigue=("mid", "high")),
        _profile("yawn_small_turn", "轻微侧身", "turn", 4.7, 0.34, drives=("curiosity",)),
        _profile("yawn_still_again", "再次静止", "stillness", 6.5, 0.16, fatigue=("high",)),
    ),
    "lean": (
        _profile("lean_approach", "主动前倾", "lean", 3.7, 0.66, drives=("possess", "crave")),
        _profile("lean_tease_tilt", "试探侧头", "sway", 3.9, 0.62, drives=("curiosity", "boredom")),
        _profile("lean_light_back", "轻快回退", "recoil", 3.8, 0.52, drives=("share",)),
        _profile("lean_greeting", "招呼靠近", "greet", 3.5, 0.58, drives=("social", "monitor")),
        _profile("lean_proud_settle", "得意回稳", "settle", 4.6, 0.42, drives=("boredom", "possess")),
    ),
    "book": (
        _profile("book_reading_pause", "阅读停顿", "settle", 5.1, 0.30, drives=("curiosity", "reflection")),
        _profile("book_thought_dip", "思索低头", "dip", 4.8, 0.38, drives=("reflection", "duty")),
        _profile("book_heard_lift", "闻声抬眼", "lift", 4.0, 0.44, drives=("monitor", "social")),
        _profile("book_small_turn", "轻微转身", "turn", 4.3, 0.40, drives=("share", "possess")),
        _profile("book_absorb_again", "重新沉浸", "breathe", 5.7, 0.28, drives=("duty", "curiosity")),
    ),
    "cup": (
        _profile("cup_slow_breath", "慢慢呼吸", "breathe", 6.0, 0.34, drives=("reflection", "grieve"), periods=("night",)),
        _profile("cup_body_sink", "身体下沉", "dip", 5.3, 0.42, drives=("grieve",), fatigue=("high",)),
        _profile("cup_hesitate_lift", "迟疑抬眼", "lift", 4.6, 0.38, drives=("monitor", "possess")),
        _profile("cup_soft_reply", "轻缓回应", "nod", 4.1, 0.34, drives=("social", "share")),
        _profile("cup_recover", "情绪回稳", "settle", 5.6, 0.30, drives=("reflection", "duty")),
    ),
}


def _derive_profiles(prefix: str, source: str) -> tuple[dict[str, Any], ...]:
    return tuple(
        {**item, "id": f"{prefix}_{str(item['id']).split('_', 1)[-1]}"}
        for item in MOTION_PROFILES[source]
    )


# The three canonical-reference figures retain the source artwork while sharing
# motion semantics with their closest existing poses. IDs remain distinct for
# telemetry and interaction cooldown accounting.
MOTION_PROFILES.update({
    "court-smile": _derive_profiles("court_smile", "playful"),
    "court-surprised": _derive_profiles("court_surprised", "letter"),
    "court-soft": _derive_profiles("court_soft", "fan"),
})


PERIOD_SPRITES = {
    "dawn": ("flowers", "cup", "yawn", "court-soft", "fan", "tea", "window", "book", "playful"),
    "day": ("court-smile", "playful", "lean", "flowers", "book", "fan", "desk", "tea", "chest"),
    "dusk": ("lantern", "steps", "court-surprised", "letter", "wistful", "tea", "cup", "fan", "desk"),
    "night": ("reading", "cup", "window", "desk", "lantern", "book", "court-soft", "wistful", "yawn"),
}

ARTIFACT_SPRITES = {
    "dream_residue": ("window", "yawn", "cup", "reading"),
    "unfinished_thread": ("tea", "steps", "cup", "fan"),
    "reflection": ("desk", "book", "chest", "wistful", "reading"),
    "letter": ("letter", "desk", "lantern", "wistful"),
    "unsent_note": ("letter", "wistful", "lantern", "fan"),
    "whisper": ("court-soft", "fan", "flowers", "lean", "playful"),
}

DRIVE_SPRITES = {
    "possess": ("lean", "cup", "wistful", "tea", "letter"),
    "monitor": ("lantern", "window", "wistful", "letter", "fan"),
    "crave": ("lean", "cup", "tea", "wistful", "steps"),
    "share": ("court-smile", "letter", "desk", "playful", "tea", "fan"),
    "libido": ("lean", "wistful", "cup", "tea", "lantern"),
    "curiosity": ("chest", "book", "reading", "lean", "flowers"),
    "boredom": ("playful", "steps", "flowers", "lean", "fan"),
    "social": ("court-smile", "playful", "tea", "steps", "fan", "letter"),
    "duty": ("desk", "book", "lantern", "chest", "reading"),
    "reflection": ("reading", "book", "desk", "window", "cup"),
    "grieve": ("cup", "wistful", "window", "reading", "flowers"),
    "anger": ("lantern", "desk", "wistful", "chest", "fan"),
}

REACTION_OPTIONS = {
    "sleepy": ("soft", "nod", "turn"),
    "melancholy": ("soft", "turn", "nod"),
    "wistful": ("turn", "soft", "nod"),
    "playful": ("lean", "greet", "nod"),
    "bright": ("greet", "lean", "nod"),
    "surprised": ("startle", "soft", "nod"),
    "curious": ("lean", "turn", "nod"),
    "focused": ("nod", "turn", "soft"),
    "absorbed": ("nod", "lift", "turn"),
    "serene": ("soft", "nod", "turn"),
    "amused": ("greet", "nod", "lean"),
    "attentive": ("turn", "nod", "greet"),
    "pensive": ("soft", "turn", "nod"),
    "gentle": ("nod", "soft", "turn"),
}


def fatigue_band(value: Any) -> str:
    try:
        amount = max(0.0, min(0.3, float(value or 0.0)))
    except (TypeError, ValueError):
        amount = 0.0
    if amount >= 0.22:
        return "high"
    if amount >= 0.10:
        return "mid"
    return "low"


def _rank_score(items: tuple[str, ...], target: str) -> float:
    try:
        index = items.index(target)
    except ValueError:
        return 0.08
    return max(0.25, 1.0 - index * 0.12)


def _hash_unit(text: str) -> float:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") / 0xFFFFFFFF


def _safe_unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def select_scene(
    *,
    scope_key: str,
    now: datetime,
    variants: dict[str, dict[str, Any]],
    top_drive_rows: list[dict[str, Any]],
    fatigue: Any,
    consciousness: str,
    newest_type: str,
    artifact_drive_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    period = "dawn" if 5 <= now.hour < 10 else "day" if 10 <= now.hour < 17 else "dusk" if 17 <= now.hour < 20 else "night"
    band = fatigue_band(fatigue)
    hold_minutes = 60 if period in {"dawn", "dusk"} else 90 if period == "day" else 75
    if band == "high":
        hold_minutes = 45
    elif consciousness == "sleeping":
        hold_minutes = 60
    slot = int(now.timestamp() // (hold_minutes * 60))
    scores: list[tuple[str, float, dict[str, float]]] = []
    for sprite in variants:
        period_score = _rank_score(PERIOD_SPRITES[period], sprite)
        if band == "high":
            fatigue_score = 1.0 if sprite in {"cup", "window", "yawn", "reading", "wistful"} else 0.16
        elif band == "mid":
            fatigue_score = 1.0 if sprite in {"tea", "reading", "cup", "desk", "steps", "fan"} else 0.45
        else:
            fatigue_score = 1.0 if sprite in {"playful", "lean", "flowers", "fan", "book", "lantern"} else 0.48
        drive_score = 0.10
        for rank, row in enumerate(top_drive_rows[:5]):
            key = str(row.get("key") or "")
            value = _safe_unit(row.get("value"))
            drive_score = max(drive_score, value * _rank_score(DRIVE_SPRITES.get(key, ()), sprite) * (1.0 - rank * 0.07))
        artifact_score = _rank_score(ARTIFACT_SPRITES.get(newest_type, ()), sprite) if newest_type else 0.20
        for row in artifact_drive_rows[:5]:
            key = str(row.get("key") or "")
            confidence = _safe_unit(row.get("confidence", row.get("value")))
            artifact_score = max(
                artifact_score,
                confidence * _rank_score(DRIVE_SPRITES.get(key, ()), sprite),
            )
        novelty_score = _hash_unit(f"{scope_key}|{slot}|{sprite}")
        total = 0.25 * period_score + 0.22 * fatigue_score + 0.25 * drive_score + 0.18 * artifact_score + 0.10 * novelty_score
        if consciousness == "sleeping":
            total *= 1.55 if sprite in {"yawn", "window", "cup"} else 0.42
        scores.append((sprite, total, {
            "period": period_score, "fatigue": fatigue_score, "drive": drive_score,
            "artifact": artifact_score, "novelty": novelty_score,
        }))
    scores.sort(key=lambda item: (-item[1], item[0]))
    selected, score, components = scores[0]
    return {
        "sprite": selected,
        "period": period,
        "fatigue_band": band,
        "hold_minutes": hold_minutes,
        "slot": slot,
        "score": round(score, 4),
        "components": {key: round(value, 4) for key, value in components.items()},
        "alternatives": [{"sprite": name, "score": round(value, 4)} for name, value, _ in scores[1:4]],
        "dominant_drives": [
            {"key": str(row.get("key") or ""), "value": round(_safe_unit(row.get("value")), 4)}
            for row in top_drive_rows[:3]
        ],
        "artifact_drives": [
            {"key": str(row.get("key") or ""), "confidence": round(_safe_unit(row.get("confidence", row.get("value"))), 4)}
            for row in artifact_drive_rows[:3]
            if str(row.get("key") or "") in DRIVE_SPRITES
        ],
    }


def motion_catalog(
    sprite: str,
    *,
    period: str,
    fatigue: str,
    newest_type: str,
    top_drive_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    drive_values = {str(row.get("key") or ""): _safe_unit(row.get("value")) for row in top_drive_rows}
    ranked: list[dict[str, Any]] = []
    for raw in MOTION_PROFILES.get(sprite, ()):
        score = 1.0
        if period in raw["periods"]:
            score += 1.15
        if fatigue in raw["fatigue"]:
            score += 0.90
        if newest_type and newest_type in raw["artifacts"]:
            score += 1.25
        score += max((drive_values.get(key, 0.0) * 1.55 for key in raw["drives"]), default=0.0)
        ranked.append({
            "id": raw["id"], "label": raw["label"], "primitive": raw["primitive"],
            "duration": raw["duration"], "amplitude": raw["amplitude"],
            "weight": round(score, 4),
        })
    ranked.sort(key=lambda item: (-item["weight"], item["id"]))
    return ranked


def select_reaction(
    expression: str,
    recent: list[dict[str, Any]],
    *,
    now: datetime,
    seed: str,
) -> dict[str, Any]:
    current = now.astimezone(timezone.utc)
    ages: list[float] = []
    recent_animations: list[str] = []
    for item in recent:
        try:
            parsed = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ages.append(max(0.0, (current - parsed.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            continue
        animation = str(item.get("reaction_key") or "").rsplit(":", 1)[-1]
        if animation:
            recent_animations.append(animation)
    last_age = min(ages) if ages else 10_000.0
    in_minute = sum(1 for age in ages if age < 60.0)
    if in_minute >= 5:
        return {"animation": "still", "tier": "quiet", "cooldown_seconds": 30, "reason": "five_click_quiet_period"}
    if last_age < 8.0:
        return {"animation": "still", "tier": "hot", "cooldown_seconds": round(8.0 - last_age, 1), "reason": "rapid_repeat"}
    options = list(REACTION_OPTIONS.get(expression, ("nod", "soft", "turn")))
    if last_age < 30.0:
        tier = "warm"
        options = [item for item in options if item in {"nod", "soft", "turn"}] or ["nod", "soft"]
    elif last_age < 180.0:
        tier = "ready"
    else:
        tier = "fresh"
    filtered = [item for item in options if item not in recent_animations[:2]] or options
    index = int(_hash_unit(seed) * len(filtered)) % len(filtered)
    return {"animation": filtered[index], "tier": tier, "cooldown_seconds": 0, "reason": "expression_and_history"}


def validate_motion_catalog() -> list[str]:
    errors: list[str] = []
    allowed = {"breathe", "sway", "turn", "nod", "settle", "startle", "recoil", "lean", "stillness", "write", "dip", "lift", "greet"}
    for sprite, profiles in MOTION_PROFILES.items():
        if len(profiles) != 5:
            errors.append(f"{sprite}: expected 5 profiles, got {len(profiles)}")
        ids = {str(item.get("id") or "") for item in profiles}
        if len(ids) != len(profiles):
            errors.append(f"{sprite}: duplicate profile ids")
        for item in profiles:
            if item.get("primitive") not in allowed:
                errors.append(f"{sprite}:{item.get('id')}: invalid primitive")
    return errors
