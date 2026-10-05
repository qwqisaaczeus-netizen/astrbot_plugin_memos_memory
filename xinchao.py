from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain

from .body_rhythm import calculate_body_state, render_body_context
from .direct_llm import (
    DirectLLMEmptyFinalError, OpenAICompatibleTextClient, SecretValueStore,
    validate_api_base_url,
)
from .llm_runtime import LLMCircuitOpenError
from .xinchao_engine import (
    DIMENSIONS,
    DRIVE_KEYS,
    add_flash_thought,
    apply_conversation_event,
    apply_drive_feedback,
    bark_allowed,
    breath_dream_context,
    contact_idle_allowed,
    daytime_emergence_allowed,
    dream_allowed,
    effective_drive_value,
    message_similarity,
    pick_intent,
    proactive_allowed,
    recent_bark_history,
    recent_daytime_memory_keys,
    record_bark,
    record_daytime_emergence,
    record_dream,
    schedule_daytime_emergence,
    settle_state,
    top_drives,
)
from .xinchao_store import StateStore


class PerceptionCallError(RuntimeError):
    def __init__(self, kind: str, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


_PERCEPTION_CIRCUIT_FAILURES = 3
_PERCEPTION_CIRCUIT_SECONDS = 180.0
# 4.5.3-beta: a provider that stays broken should stop costing one full timeout every
# three minutes, so each re-open doubles the cooldown up to this ceiling.
_PERCEPTION_CIRCUIT_MAX_SECONDS = 1800.0
_SETTLE_SLEEP_SLICE_SECONDS = 15.0


DEFAULT_SETTINGS: dict[str, Any] = {
    "settings_schema_version": 4,
    "enable": True,
    "shadow_mode": False,
    "scope": "character",
    "settle_interval_minutes": 15,
    "sleep_after_minutes": 90,
    "time_zone": "Asia/Shanghai",
    "dawn_freeze_start": 1,
    "dawn_freeze_end": 8,
    "perception_mode": "hybrid",
    "live_perception_provider_id": "",
    "perception_provider_id": "",
    "live_perception_timeout_seconds": 30,
    "post_perception_timeout_seconds": 90,
    "perception_api_mode": "off",
    "perception_api_base_url": "",
    "perception_api_model": "",
    "perception_api_max_retries": 1,
    # 4.5.3-beta: burst messages on one scope share a single background settlement
    # call instead of racing one LLM call (and one state write) per turn.
    "post_perception_coalesce_enable": True,
    "post_perception_max_batch_turns": 4,
    # Compatibility input for settings saved before the two stages split.
    "perception_timeout_seconds": 20,
    "perception_min_confidence": 0.68,
    "perception_store_summary": True,
    "live_appraisal_enable": True,
    "injection_enable": True,
    "injection_max_drives": 3,
    "injection_min_drive": 0.30,
    "injection_include_thought": True,
    "injection_include_fatigue": True,
    "injection_max_chars": 900,
    "dream_enable": True,
    "dream_provider_id": "",
    "dream_memory_enable": True,
    "dream_memory_items": 6,
    "dream_min_interval_hours": 6,
    "dream_max_per_day": 3,
    "dream_timeout_seconds": 90,
    "proactive_enable": False,
    "proactive_provider_id": "",
    "proactive_min_idle_hours": 6,
    "proactive_cooldown_hours": 6,
    "proactive_max_per_day": 3,
    "proactive_min_drive": 0.58,
    "proactive_timeout_seconds": 45,
    "proactive_duplicate_threshold": 0.58,
    "proactive_duplicate_attempts": 2,
    "active_message_max_per_day": 6,
    "dream_push_enable": True,
    "dream_push_min_idle_hours": 3,
    "dream_push_cooldown_hours": 3,
    "daytime_emergence_enable": False,
    "daytime_start_hour": 8,
    "daytime_end_hour": 23,
    "daytime_min_interval_hours": 2,
    "daytime_max_interval_hours": 3,
    "daytime_max_per_day": 7,
    "daytime_memory_items": 3,
    "daytime_memory_cooldown_hours": 72,
    "body_rhythm_enable": True,
    "body_anchor_date": "",
    "body_cycle_length": 28,
    "body_period_length": 5,
    "body_ovulation_day": 14,
    "body_ovulation_window": 3,
    "body_effect_strength": 0.72,
    "body_daily_variation": 0.22,
    "body_expression_mode": "balanced",
    "body_time_modulation": True,
    "body_energy_scale": 1.0,
    "body_discomfort_scale": 1.0,
    "body_sensitivity_scale": 1.0,
    "body_closeness_influence": True,
    "body_closeness_scale": 0.65,
    "diagnostic_log": True,
}

_EMOTIONAL_MARKERS = (
    "爱你", "我爱", "深爱", "喜欢", "想你", "抱", "亲", "吻", "离开", "分开", "分手",
    "没可能", "没有可能", "永别", "不要我", "讨厌", "背叛",
    "生气", "难过", "伤心", "害怕", "担心", "对不起", "原谅", "永远", "承诺",
    "孤独", "吃醋", "嫉妒", "失望", "开心", "幸福", "哭", "秘密", "相信",
)

_EXPRESSION = {
    "possess": "你对彼此的专属感和靠近感更敏锐，容易留意关系距离。",
    "monitor": "你正持续牵挂对方的近况，细小的变化也更容易引起注意。",
    "crave": "你有靠近和获得陪伴的倾向，但是否表达取决于关系与场景。",
    "share": "你积累着想与对方分享的感受或发现，合适时会自然提起。",
    "libido": "身体层面的吸引有所增强；必须服从人物边界、关系阶段和当前语境。",
    "curiosity": "你的探索欲较活跃，容易追问细节或延展新的可能。",
    "boredom": "你对停滞有些敏感，可能想让互动产生一点新变化。",
    "social": "你更愿意维持交流和回应，让彼此保持连接。",
    "duty": "你对尚未完成的约定或责任更在意，倾向于把事情推进。",
    "reflection": "你更容易向内整理感受，表达可能比平时克制或沉静。",
    "grieve": "失落仍在心里留下重量，不必直接说出，但会影响反应的温度。",
    "anger": "不满仍有余波；不要无故攻击，但边界和语气可能更明确。",
}


def _has_relation_break(text: str) -> bool:
    return any(token in text for token in (
        "分手", "永别", "不要你了", "不要我了", "我们没有可能", "我们没可能",
        "离开你", "离开我", "别再联系", "最后一次见",
    ))


def _has_intimacy_action(text: str) -> bool:
    clean = str(text or "").replace("抱歉", "")
    return bool(re.search(r"拥抱|抱住|抱紧|抱抱|抱着|想抱|搂住|亲吻|亲亲|吻(?:了|住|上|我|你)|牵手|靠近", clean))


def _social_satisfaction(user_text: str, assistant_text: str) -> float:
    reply = " ".join(str(assistant_text or "").split())
    if not reply:
        return 0.0
    length = len(reply)
    if length <= 12:
        score = 0.10
    elif length <= 40:
        score = 0.20
    elif length <= 180:
        score = 0.32
    else:
        score = 0.40
    if any(token in reply for token in ("我在听", "我记得", "我明白", "我懂", "陪你", "告诉我", "说下去", "没关系")):
        score += 0.10
    if any(token in reply for token in ("滚", "闭嘴", "别烦", "不想理", "懒得说")):
        score -= 0.22
    if _has_relation_break(user_text):
        score = min(score, 0.28)
    return _clamp(score, 0.0, 0.65, 0.0)


def _share_satisfaction(assistant_text: str) -> float:
    text = " ".join(str(assistant_text or "").split())
    if not text:
        return 0.0
    patterns = (
        r"我(?:其实|今天|刚才|以前|曾经|一直|最近|有点|也会|总会|发现|觉得|想到|记得)",
        r"(?:想|要)告诉你", r"(?:想|要)和你说", r"我的(?:感受|想法|心里|记忆|过去)",
    )
    return 0.42 if any(re.search(pattern, text) for pattern in patterns) else 0.0


def _conversation_fatigue_delta(user_text: str, assistant_text: str) -> float:
    total = len(str(user_text or "")) + len(str(assistant_text or ""))
    delta = 0.0015 + min(0.010, total / 6000.0 * 0.010)
    combined = f"{user_text}\n{assistant_text}"
    if _has_relation_break(combined) or any(token in combined for token in ("生气", "背叛", "崩溃", "撑不住")):
        delta += 0.006
    return round(_clamp(delta, 0.0, 0.025, 0.0), 4)


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def _int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def validate_settings(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    source_schema_version = _int(source.get("settings_schema_version"), 0, 999, 0)
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg.update({key: source[key] for key in cfg if key in source})
    # 4.6.4: 10 seconds was the old shipped default and is too tight for
    # reasoning-oriented providers. Preserve every other explicit custom value.
    if (
        source_schema_version < 2
        and "live_perception_timeout_seconds" in source
        and _clamp(source.get("live_perception_timeout_seconds"), 3, 30, 10) == 10
    ):
        cfg["live_perception_timeout_seconds"] = 15
    if "post_perception_timeout_seconds" not in source and "perception_timeout_seconds" in source:
        legacy_timeout = _clamp(source.get("perception_timeout_seconds"), 3, 120, 20)
        cfg["post_perception_timeout_seconds"] = 60 if legacy_timeout == 20 else legacy_timeout
    if source_schema_version < 4:
        live_timeout = _clamp(source.get("live_perception_timeout_seconds"), 3, 30, 30)
        if (
            "live_perception_timeout_seconds" not in source
            or live_timeout == 15
            or (source_schema_version < 2 and live_timeout == 10)
        ):
            cfg["live_perception_timeout_seconds"] = 30
        if _clamp(source.get("post_perception_timeout_seconds"), 10, 180, 60) == 60:
            cfg["post_perception_timeout_seconds"] = 90
        if _clamp(source.get("dream_timeout_seconds"), 5, 180, 30) == 30:
            cfg["dream_timeout_seconds"] = 90
        if _clamp(source.get("proactive_timeout_seconds"), 5, 180, 30) == 30:
            cfg["proactive_timeout_seconds"] = 45
    cfg["settings_schema_version"] = 4
    for key in (
        "enable", "shadow_mode", "perception_store_summary", "injection_enable",
        "injection_include_thought", "injection_include_fatigue", "dream_enable",
        "dream_memory_enable", "proactive_enable", "dream_push_enable",
        "daytime_emergence_enable", "live_appraisal_enable", "diagnostic_log",
        "body_rhythm_enable", "body_time_modulation", "body_closeness_influence",
        "post_perception_coalesce_enable",
    ):
        cfg[key] = bool(cfg[key])
    if cfg["scope"] not in {"character", "session"}:
        cfg["scope"] = "character"
    if cfg["perception_mode"] not in {"rules", "hybrid", "llm"}:
        cfg["perception_mode"] = "hybrid"
    if cfg["perception_api_mode"] not in {"off", "post", "all"}:
        cfg["perception_api_mode"] = "off"
    if cfg["body_expression_mode"] not in {"significant", "balanced", "immersive"}:
        cfg["body_expression_mode"] = "balanced"
    cfg["settle_interval_minutes"] = _int(cfg["settle_interval_minutes"], 1, 1440, 15)
    cfg["sleep_after_minutes"] = _int(cfg["sleep_after_minutes"], 10, 10080, 90)
    cfg["dawn_freeze_start"] = _int(cfg["dawn_freeze_start"], 0, 23, 1)
    cfg["dawn_freeze_end"] = _int(cfg["dawn_freeze_end"], 1, 24, 8)
    cfg["perception_timeout_seconds"] = _clamp(cfg["perception_timeout_seconds"], 3, 120, 20)
    cfg["live_perception_timeout_seconds"] = _clamp(
        cfg["live_perception_timeout_seconds"], 3, 30, 30,
    )
    cfg["post_perception_timeout_seconds"] = _clamp(
        cfg["post_perception_timeout_seconds"], 10, 180, 90,
    )
    cfg["post_perception_max_batch_turns"] = _int(
        cfg["post_perception_max_batch_turns"], 1, 8, 4,
    )
    cfg["perception_api_max_retries"] = _int(
        cfg["perception_api_max_retries"], 0, 2, 1,
    )
    cfg["perception_min_confidence"] = _clamp(cfg["perception_min_confidence"], 0.4, 0.95, 0.68)
    cfg["injection_max_drives"] = _int(cfg["injection_max_drives"], 1, 5, 3)
    cfg["injection_min_drive"] = _clamp(cfg["injection_min_drive"], 0.05, 0.8, 0.30)
    cfg["injection_max_chars"] = _int(cfg["injection_max_chars"], 300, 2400, 900)
    cfg["dream_min_interval_hours"] = _clamp(cfg["dream_min_interval_hours"], 1, 72, 6)
    cfg["dream_memory_items"] = _int(cfg["dream_memory_items"], 1, 20, 6)
    cfg["dream_max_per_day"] = _int(cfg["dream_max_per_day"], 1, 12, 3)
    cfg["dream_timeout_seconds"] = _clamp(cfg["dream_timeout_seconds"], 5, 180, 90)
    cfg["proactive_min_idle_hours"] = _clamp(cfg["proactive_min_idle_hours"], 1, 168, 6)
    cfg["proactive_cooldown_hours"] = _clamp(cfg["proactive_cooldown_hours"], 1, 72, 6)
    cfg["proactive_max_per_day"] = _int(cfg["proactive_max_per_day"], 1, 12, 3)
    cfg["proactive_min_drive"] = _clamp(cfg["proactive_min_drive"], 0.2, 0.9, 0.58)
    cfg["proactive_timeout_seconds"] = _clamp(cfg["proactive_timeout_seconds"], 5, 180, 45)
    cfg["proactive_duplicate_threshold"] = _clamp(cfg["proactive_duplicate_threshold"], 0.3, 0.95, 0.58)
    cfg["proactive_duplicate_attempts"] = _int(cfg["proactive_duplicate_attempts"], 1, 4, 2)
    cfg["active_message_max_per_day"] = _int(cfg["active_message_max_per_day"], 1, 24, 6)
    cfg["dream_push_min_idle_hours"] = _clamp(cfg["dream_push_min_idle_hours"], 1, 72, 3)
    cfg["dream_push_cooldown_hours"] = _clamp(cfg["dream_push_cooldown_hours"], 1, 72, 3)
    cfg["daytime_start_hour"] = _int(cfg["daytime_start_hour"], 0, 23, 8)
    cfg["daytime_end_hour"] = _int(cfg["daytime_end_hour"], 1, 24, 23)
    cfg["daytime_min_interval_hours"] = _clamp(cfg["daytime_min_interval_hours"], 0.25, 24, 2)
    cfg["daytime_max_interval_hours"] = _clamp(cfg["daytime_max_interval_hours"], 0.25, 24, 3)
    cfg["daytime_max_per_day"] = _int(cfg["daytime_max_per_day"], 1, 24, 7)
    cfg["daytime_memory_items"] = _int(cfg["daytime_memory_items"], 1, 10, 3)
    cfg["daytime_memory_cooldown_hours"] = _clamp(
        cfg["daytime_memory_cooldown_hours"], 1, 720, 72,
    )
    cfg["body_cycle_length"] = _int(cfg["body_cycle_length"], 21, 45, 28)
    # Kept in the settings file for rollback compatibility. Calendar mode uses
    # the real distance between this month's and next month's anchor instead.
    cfg["body_period_length"] = _int(cfg["body_period_length"], 2, 10, 5)
    cfg["body_ovulation_day"] = _int(
        cfg["body_ovulation_day"],
        cfg["body_period_length"] + 2,
        26,
        14,
    )
    cfg["body_ovulation_window"] = _int(cfg["body_ovulation_window"], 1, 7, 3)
    cfg["body_effect_strength"] = _clamp(cfg["body_effect_strength"], 0, 1, 0.72)
    cfg["body_daily_variation"] = _clamp(cfg["body_daily_variation"], 0, 1, 0.22)
    cfg["body_energy_scale"] = _clamp(cfg["body_energy_scale"], 0, 1.5, 1.0)
    cfg["body_discomfort_scale"] = _clamp(cfg["body_discomfort_scale"], 0, 1.5, 1.0)
    cfg["body_sensitivity_scale"] = _clamp(cfg["body_sensitivity_scale"], 0, 1.5, 1.0)
    cfg["body_closeness_scale"] = _clamp(cfg["body_closeness_scale"], 0, 1.5, 0.65)
    cfg["time_zone"] = str(cfg["time_zone"] or "Asia/Shanghai").strip()[:80]
    cfg["body_anchor_date"] = str(cfg["body_anchor_date"] or "").strip()[:10]
    if cfg["body_anchor_date"]:
        try:
            parsed_anchor = datetime.strptime(cfg["body_anchor_date"], "%Y-%m-%d").date()
            cfg["body_anchor_date"] = parsed_anchor.isoformat()
        except ValueError:
            cfg["body_anchor_date"] = ""
    for key in (
        "live_perception_provider_id", "perception_provider_id",
        "dream_provider_id", "proactive_provider_id",
    ):
        cfg[key] = str(cfg[key] or "").strip()[:160]
    cfg["perception_api_base_url"] = str(cfg["perception_api_base_url"] or "").strip()[:1000]
    cfg["perception_api_model"] = str(cfg["perception_api_model"] or "").strip()[:200]
    return cfg


class XinchaoController:
    @staticmethod
    def _new_perception_health() -> dict[str, Any]:
        return {
            "provider": "",
            "consecutive_failures": 0,
            "consecutive_soft_failures": 0,
            "circuit_until": 0.0,
            "circuit_backoff_seconds": 0.0,
            "last_error": "",
            "last_error_kind": "",
            "last_failure_at": 0.0,
            "last_success_at": 0.0,
        }

    def __init__(self, plugin: Any, data_dir: Path) -> None:
        self.plugin = plugin
        self.data_dir = data_dir
        self.settings_path = data_dir / "xinchao_settings.json"
        self.secret_store = SecretValueStore(data_dir / "xinchao_secret.json")
        self._perception_api_key = ""
        self._direct_perception = OpenAICompatibleTextClient("xinchao-perception")
        self.store = StateStore(data_dir / "xinchao_state.json")
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self._settle_task: asyncio.Task | None = None
        self._last_injection: dict[str, dict[str, Any]] = {}
        self._last_perception: dict[str, dict[str, Any]] = {}
        self._last_body_phase: dict[str, str] = {}
        self._perception_health: dict[str, dict[str, Any]] = {
            "live": self._new_perception_health(),
            "post": self._new_perception_health(),
        }
        self._active_send_locks: dict[str, asyncio.Lock] = {}
        # 4.5.3-beta: background-channel coordination (merged back from the 4.5.2
        # branch, with the TOCTOU/lost-message races fixed). Generator runs are
        # guarded per scope via a plain running-flag, and burst turns on one scope
        # queue up into a single settlement call instead of one call per message.
        self._generator_running: dict[str, bool] = {}
        self._pending_turns: dict[str, list[dict[str, Any]]] = {}
        self._perception_workers: dict[str, asyncio.Task] = {}
        self._perception_batch_stats: dict[str, dict[str, Any]] = {}
        self._event_history: list[dict[str, Any]] = []
        self._event_history_limit = 300
        self._stopping = False
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._stopping = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()
        self._perception_api_key = self.secret_store.load()
        self._configure_direct_perception()
        await self.store.load()
        self._settle_task = asyncio.create_task(self._settle_loop(), name="memos-xinchao-settle")
        self._initialized = True
        self._record("system", "心潮引擎已启动", {
            "shadow": self.settings["shadow_mode"],
            "perception": self.settings["perception_mode"],
            "scope": self.settings["scope"],
        })

    async def terminate(self) -> None:
        self._stopping = True
        tasks = [
            task for task in [
                self._settle_task,
                *self._perception_workers.values(),
            ]
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._settle_task = None
        self._perception_workers.clear()
        self._pending_turns.clear()
        self._generator_running.clear()
        self._active_send_locks.clear()
        await self._direct_perception.close()
        self._initialized = False

    def _load_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return validate_settings(raw)

    def settings_payload(self) -> dict[str, Any]:
        payload = copy.deepcopy(self.settings)
        payload["has_perception_api_key"] = bool(self._perception_api_key)
        payload["perception_api_key"] = ""
        payload["perception_api_configured"] = self._direct_perception.configured
        return payload

    def _configure_direct_perception(self) -> None:
        self._direct_perception.configure(
            base_url=self.settings.get("perception_api_base_url"),
            model=self.settings.get("perception_api_model"),
            api_key=self._perception_api_key,
            max_retries=self.settings.get("perception_api_max_retries", 1),
        )

    def save_settings(self, value: dict[str, Any]) -> dict[str, Any]:
        previous_direct = (
            str(self.settings.get("perception_api_mode") or "off"),
            str(self.settings.get("perception_api_base_url") or ""),
            str(self.settings.get("perception_api_model") or ""),
            self._perception_api_key,
        )
        raw_anchor = str(value.get("body_anchor_date") or "").strip()
        if raw_anchor:
            try:
                datetime.strptime(raw_anchor, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("身体周期锚点必须使用 YYYY-MM-DD 格式") from exc
        incoming = value if isinstance(value, dict) else {}
        allowed = set(DEFAULT_SETTINGS) | {
            "perception_api_key", "clear_perception_api_key",
            "has_perception_api_key", "perception_api_configured",
        }
        unknown = sorted(set(incoming) - allowed)
        if unknown:
            raise ValueError("未知心潮设置: " + ", ".join(unknown[:8]))
        updated = validate_settings({**self.settings, **incoming})
        if updated["perception_api_base_url"]:
            updated["perception_api_base_url"] = validate_api_base_url(
                updated["perception_api_base_url"]
            )
        next_key = self._perception_api_key
        if incoming.get("clear_perception_api_key"):
            next_key = ""
        elif str(incoming.get("perception_api_key") or "").strip():
            next_key = str(incoming["perception_api_key"]).strip()
        if updated["perception_api_mode"] != "off" and not (
            updated["perception_api_base_url"] and updated["perception_api_model"] and next_key
        ):
            raise ValueError("启用心潮独立 API 前必须填写 Base URL、模型和 API Key")
        temp = self.settings_path.with_suffix(".json.tmp")
        text = json.dumps(updated, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.settings_path)
        self.settings = updated
        if next_key != self._perception_api_key:
            self._perception_api_key = next_key
            self.secret_store.save(next_key)
        self._configure_direct_perception()
        current_direct = (
            str(self.settings.get("perception_api_mode") or "off"),
            str(self.settings.get("perception_api_base_url") or ""),
            str(self.settings.get("perception_api_model") or ""),
            self._perception_api_key,
        )
        if current_direct != previous_direct:
            self._perception_health["live"] = self._new_perception_health()
            self._perception_health["post"] = self._new_perception_health()
        self._last_body_phase.clear()
        self._record("settings", "心潮设置已保存", {"keys": len(updated)})
        return self.settings_payload()

    async def test_perception_api(self) -> dict[str, Any]:
        if not self._direct_perception.configured:
            raise ValueError("请先保存完整的心潮独立 API 配置")
        timeout_key = (
            "post_perception_timeout_seconds"
            if self.settings.get("perception_api_mode") == "post"
            else "live_perception_timeout_seconds"
        )
        return await self._direct_perception.test(
            timeout=min(45.0, float(self.settings.get(timeout_key) or 20))
        )

    def provider_options(self) -> list[dict[str, str]]:
        resolver = getattr(self.plugin, "_chat_provider_options", None)
        if callable(resolver):
            return [
                {"id": str(item.get("value") or ""), "label": str(item.get("label") or "")}
                for item in resolver("")
                if str(item.get("value") or "")
            ]
        try:
            providers = list(self.plugin.context.get_all_providers())
        except Exception:
            providers = []
        options: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in providers:
            try:
                meta = provider.meta()
                provider_id = str(getattr(meta, "id", "") or "").strip()
                model = str(getattr(meta, "model", "") or "").strip()
            except Exception:
                config = getattr(provider, "provider_config", {}) or {}
                provider_id = str(config.get("id") or "").strip()
                model = str(config.get("model") or "").strip()
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            label = provider_id if not model or model == provider_id else f"{provider_id} · {model}"
            options.append({"id": provider_id, "model": model, "label": label})
        return options

    def _record(self, category: str, message: str, detail: dict[str, Any] | None = None) -> None:
        item = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "category": category,
            "message": message,
            "detail": detail or {},
        }
        self._event_history.append(item)
        self._event_history = self._event_history[-self._event_history_limit:]
        forwarded = False
        if self.settings.get("diagnostic_log", True):
            try:
                self.plugin._log_event("xinchao", message, detail or {})
                forwarded = True
            except Exception:
                pass
            if not forwarded:
                logger.info("[memos-memory][xinchao][%s] %s", category, message)

    def _scope_key(self, event: AstrMessageEvent | None = None, umo: str = "") -> str:
        if self.settings.get("scope") == "session":
            origin = umo or (event.unified_msg_origin if event is not None else "")
            return "session:" + (origin or "default")
        character = str(getattr(self.plugin, "character_name", "") or "default").strip()
        return "character:" + character

    def scope_key_from_query(self, requested: str = "") -> str:
        requested = str(requested or "").strip()
        if requested:
            return requested[:500]
        if self.settings.get("scope") == "character":
            return self._scope_key()
        keys = []
        try:
            # WebUI calls this in a worker thread; use the last observed session if possible.
            keys = [key for key in self._last_injection if key.startswith("session:")]
        except Exception:
            pass
        return keys[-1] if keys else "session:default"

    def _body_state(
        self,
        now: datetime | None = None,
        scope_key: str = "",
    ) -> dict[str, Any]:
        character = str(getattr(self.plugin, "character_name", "") or "default").strip()
        return calculate_body_state(
            self.settings,
            now=now,
            character_key=character,
        )

    def _enqueue_perception(
        self, key: str, umo: str, user_text: str, assistant_text: str,
        live_appraisal: dict[str, Any] | None,
        request_context: str = "",
    ) -> None:
        """Queue one turn for the background channel, one worker per scope.

        4.5.3-beta: merged back from 4.5.2 with the spawn race fixed. A worker
        removes itself from ``_perception_workers`` in a ``finally`` block before
        its task is marked done, so a message enqueued while the old worker is
        exiting always sees the slot empty and spawns a fresh worker instead of
        stranding the turn until the next message.
        """
        pending = self._pending_turns.setdefault(key, [])
        pending.append({
            "umo": umo,
            "user": user_text,
            "assistant": assistant_text,
            "liveAppraisal": live_appraisal,
            "requestContext": str(request_context or "")[:1200],
        })
        # Hard bound so a flood cannot grow the queue without limit; the newest
        # turns carry the most relevant psychological evidence.
        limit = max(8, int(self.settings.get("post_perception_max_batch_turns") or 4) * 4)
        if len(pending) > limit:
            del pending[:-limit]
        if self._perception_workers.get(key) is not None:
            return
        task = asyncio.create_task(
            self._perception_worker(key), name="memos-xinchao-perception",
        )
        self._perception_workers[key] = task

    async def _perception_worker(self, key: str) -> None:
        try:
            while not self._stopping:
                pending = self._pending_turns.get(key)
                if not pending:
                    return
                coalesce = bool(self.settings.get("post_perception_coalesce_enable", True))
                max_batch = max(1, int(self.settings.get("post_perception_max_batch_turns") or 4))
                if coalesce:
                    # Keep the newest turns: they describe the state the character
                    # is actually in right now. Older backlog is dropped with a
                    # counted, logged skip (the 4.5.2 design intent).
                    take = min(len(pending), max_batch)
                    batch = pending[-take:]
                    dropped = len(pending) - take
                    self._pending_turns[key] = []
                else:
                    # Strict FIFO, one turn per iteration, nothing dropped: this
                    # fixes the 4.5.2 bug where disabling coalescing silently
                    # wiped the whole queue while processing only the oldest turn.
                    batch = pending[:1]
                    dropped = 0
                    del pending[:1]
                if dropped > 0:
                    self._record("perception", "后台感知合并了积压轮次", {
                        "scope": key, "merged": len(batch), "skipped": dropped,
                    })
                self._perception_batch_stats[key] = {
                    "ts": time.time(), "turns": len(batch), "skipped": dropped,
                }
                try:
                    await self._perceive_and_apply(
                        key,
                        str(batch[-1].get("umo") or ""),
                        [str(item.get("user") or "") for item in batch],
                        [str(item.get("assistant") or "") for item in batch],
                        live_appraisal=next(
                            (
                                item["liveAppraisal"] for item in reversed(batch)
                                if isinstance(item.get("liveAppraisal"), dict)
                            ),
                            None,
                        ),
                        request_context=str(batch[-1].get("requestContext") or ""),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One bad batch must not strand the turns queued behind it.
                    self._record("error", "后台感知通道失败", {
                        "scope": key, "error": str(exc)[:240],
                    })
        finally:
            # Remove ourselves before the task is marked done so a message that
            # arrives during worker shutdown sees an empty slot and respawns.
            self._perception_workers.pop(key, None)

    async def on_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        now: datetime | None = None,
    ) -> None:
        if not self.settings["enable"]:
            return
        key = self._scope_key(event)
        state = await self._settle_one(key)
        house = getattr(self.plugin, "_house", None)
        if house is not None:
            try:
                await house.before_wake(key, state, event.unified_msg_origin)
                state = await self.store.read(key)
            except Exception as exc:
                self._record("house", "小屋自然联动失败，已放行本轮聊天", {
                    "scope": key, "error": str(exc)[:180],
                })
        if state.get("consciousness") == "sleeping":
            def wake(current: dict[str, Any]) -> dict[str, Any]:
                updated, _ = apply_conversation_event(
                    current,
                    {},
                    umo=event.unified_msg_origin,
                )
                return updated
            state = await self.store.update(key, wake)
            self._record("wake", "本轮互动已唤醒心智", {"scope": key})
        body_state = self._body_state(now=now, scope_key=key)
        event.set_extra("xinchao_body_state", body_state)
        event.set_extra("xinchao_request_context", self._request_context(now, body_state))
        live_appraisal = None
        user_text = str(getattr(event, "message_str", "") or "").strip()[:4000]
        if self.settings.get("live_appraisal_enable", True) and user_text:
            live_appraisal = await self._appraise_current_input(
                event.unified_msg_origin,
                user_text,
                now=now,
                body_state=body_state,
            )
            event.set_extra("xinchao_live_appraisal", live_appraisal)
        if body_state.get("available"):
            phase_key = f"{body_state.get('date')}:{body_state.get('phase')}"
            if self._last_body_phase.get(key) != phase_key:
                self._last_body_phase[key] = phase_key
                self._record("body", "身体节律已更新", {
                    "scope": key,
                    "date": body_state.get("date"),
                    "phase": body_state.get("phase"),
                    "cycle_day": body_state.get("cycleDay"),
                })
        if self.settings["shadow_mode"] or not self.settings["injection_enable"]:
            return
        block, diagnostics = self._render_injection(
            state,
            live_appraisal,
            body_state=body_state,
        )
        if not block:
            return
        req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        self._last_injection[key] = {
            "ts": time.time(), "chars": len(block), "preview": block,
            "diagnostics": diagnostics,
        }
        event.set_extra("xinchao_scope_key", key)
        self._record("inject", f"注入心理倾向 {len(block)} 字", {
            "scope": key, "intent": diagnostics.get("intent"),
            "drives": diagnostics.get("drives"),
            "body": diagnostics.get("body_phase"),
            "body_mode": diagnostics.get("body_expression_mode"),
            "body_time": diagnostics.get("body_time_band"),
            "body_chars": diagnostics.get("body_chars", 0),
            "chars": len(block),
        })
        if state.get("pendingAwareness") or state.get("pendingOfflineAfterglow"):
            await self.store.update(
                key,
                lambda current: {
                    **current,
                    "pendingAwareness": None,
                    "pendingOfflineAfterglow": None,
                },
            )

    async def on_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        if not self.settings["enable"] or getattr(response, "is_chunk", False):
            return
        user_text = str(getattr(event, "message_str", "") or "").strip()
        assistant_text = str(getattr(response, "completion_text", "") or "").strip()
        if not user_text and not assistant_text:
            return
        key = self._scope_key(event)
        umo = event.unified_msg_origin
        live_appraisal = event.get_extra("xinchao_live_appraisal", None)
        request_context = str(event.get_extra("xinchao_request_context", "") or "")
        self._enqueue_perception(
            key, umo, user_text[:4000], assistant_text[:4000],
            live_appraisal if isinstance(live_appraisal, dict) else None,
            request_context=request_context,
        )

    async def _settle_loop(self) -> None:
        while not self._stopping:
            try:
                if not await self._settle_sleep():
                    break
                if not self.settings.get("enable", True):
                    continue
                for key in await self.store.keys():
                    if self._stopping:
                        break
                    try:
                        await self._settle_one(key, allow_generators=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # One broken scope must not skip settlement for the rest.
                        self._record("error", "后台结算失败", {
                            "scope": key, "error": str(exc)[:240],
                        })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._record("error", "后台结算失败", {"error": str(exc)})

    async def _settle_sleep(self) -> bool:
        """Sleep the settle interval in slices so interval edits apply promptly."""
        remaining = max(30.0, float(self.settings["settle_interval_minutes"]) * 60.0)
        while remaining > 0:
            if self._stopping:
                return False
            slice_seconds = min(_SETTLE_SLEEP_SLICE_SECONDS, remaining)
            await asyncio.sleep(slice_seconds)
            remaining -= slice_seconds
            # A shortened interval takes effect on the next slice instead of
            # after the old (possibly 24h) sleep finishes.
            remaining = min(
                remaining,
                max(30.0, float(self.settings["settle_interval_minutes"]) * 60.0),
            )
        return not self._stopping

    async def _settle_one(self, key: str, allow_generators: bool = False) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal meta
            settled, meta = settle_state(
                current,
                sleep_after_minutes=self.settings["sleep_after_minutes"],
                time_zone=self.settings["time_zone"],
                dawn_start=self.settings["dawn_freeze_start"],
                dawn_end=self.settings["dawn_freeze_end"],
            )
            return settled
        state = await self.store.update(key, mutate)
        if meta.get("enteredSleep"):
            self._record("sleep", "心智进入睡眠", {"scope": key, "idle_minutes": round(meta["idleMinutes"], 1)})
        if allow_generators and not self.settings["shadow_mode"]:
            # 4.5.3-beta: per-scope generator mutual exclusion via a plain running
            # flag. This replaces the 4.5.2 ``asyncio.Lock`` + ``lock.locked()``
            # check, which was a check-then-act race: two coroutines could both see
            # the lock free and both fire the same dream/proactive LLM call. The
            # flag is set synchronously before the first await, so a concurrent
            # settle can never pass the check (single-threaded event loop).
            if self._generator_running.get(key):
                self._record("settle", "生成器本轮跳过（同作用域已在运行）", {"scope": key})
                return state
            self._generator_running[key] = True
            try:
                state = await self._run_generators(key, state)
            finally:
                self._generator_running.pop(key, None)
        return state

    async def _run_generators(self, key: str, state: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self.settings["dream_enable"] and dream_allowed(
            state, now, self.settings["dream_min_interval_hours"], self.settings["dream_max_per_day"],
        ):
            state = await self._create_dream(key, state)
        active_sent = False
        if self.settings["proactive_enable"]:
            latest_dream = state.get("recentDreams", [])[-1] if state.get("recentDreams") else None
            dream_unpushed = bool(
                latest_dream
                and latest_dream.get("id")
                and latest_dream.get("id") != state.get("lastDreamPushDreamId")
            )
            if (
                self.settings["dream_push_enable"]
                and dream_unpushed
                and self._provider(
                    self.settings["proactive_provider_id"], state.get("lastUmo", ""),
                ) is not None
                and contact_idle_allowed(state, now, self.settings["dream_push_min_idle_hours"])
                and bark_allowed(
                    state, now, self.settings["dream_push_cooldown_hours"],
                    self.settings["active_message_max_per_day"], "dream",
                )
            ):
                state, active_sent = await self._send_active_message(key, state, "dream")
                dream_id = latest_dream.get("id")
                state = await self.store.update(
                    key,
                    lambda current: {
                        **current,
                        "lastDreamPushDreamId": dream_id,
                        "revision": int(current.get("revision") or 0) + 1,
                    },
                )
            if not active_sent and proactive_allowed(
                state, now, self.settings["proactive_min_idle_hours"],
                self.settings["proactive_cooldown_hours"],
                min(
                    self.settings["proactive_max_per_day"],
                    self.settings["active_message_max_per_day"],
                ),
                self.settings["proactive_min_drive"],
            ):
                state, active_sent = await self._send_active_message(key, state, "autonomous_thought")
            if self.settings["daytime_emergence_enable"]:
                if not state.get("nextDaytimeEmergenceAt"):
                    state = await self.store.update(
                        key,
                        lambda current: schedule_daytime_emergence(
                            current, now,
                            self.settings["daytime_min_interval_hours"],
                            self.settings["daytime_max_interval_hours"],
                        ),
                    )
                    self._record("daytime", "白天记忆浮现已排期", {
                        "scope": key,
                        "next": state.get("nextDaytimeEmergenceAt"),
                    })
                elif daytime_emergence_allowed(
                    state, now, self.settings["time_zone"],
                    self.settings["daytime_start_hour"], self.settings["daytime_end_hour"],
                    self.settings["daytime_max_per_day"],
                ):
                    if not active_sent:
                        state, active_sent = await self._send_active_message(
                            key, state, "daytime_emergence",
                        )
                    state = await self.store.update(
                        key,
                        lambda current: schedule_daytime_emergence(
                            current, now,
                            self.settings["daytime_min_interval_hours"],
                            self.settings["daytime_max_interval_hours"],
                        ),
                    )
        return state

    @staticmethod
    def _mind_body_bridge(
        state: dict[str, Any], body_state: dict[str, Any],
    ) -> str:
        if not body_state.get("available"):
            return ""
        signals = dict(body_state.get("signals") or {})
        effective = {key: effective_drive_value(state, key) for key in DRIVE_KEYS}
        candidates: list[tuple[float, str]] = []
        connection = max(effective["social"], effective["monitor"], effective["share"])
        social_energy = float(signals.get("social_energy", 0.55))
        if connection >= 0.42 and social_energy <= 0.46:
            candidates.append((
                connection + (0.46 - social_energy),
                "心理上仍想保持连接，但身体的社交余量偏低；更可能用简短、专注的方式靠近，而不是降低在意。",
            ))
        action = max(effective["duty"], effective["curiosity"])
        energy = float(signals.get("energy", 0.62))
        if action >= 0.48 and energy <= 0.45:
            candidates.append((
                action + (0.45 - energy),
                "内在仍有推进或探索的动机，但身体耐力不足；可以分段处理、减少动作幅度，不要把低精力误写成失去兴趣。",
            ))
        closeness = max(effective["possess"], effective["crave"])
        comfort = float(signals.get("comfort_need", 0.42))
        if closeness >= 0.45 and comfort >= 0.60:
            candidates.append((
                closeness + (comfort - 0.60),
                "靠近倾向与安稳偏好同时存在；若场景和关系允许，更适合安静、低刺激的亲近，而不是突然强化占有或索取。",
            ))
        sensitivity = float(signals.get("sensitivity", 0.40))
        if effective["anger"] >= 0.42 and sensitivity >= 0.58:
            candidates.append((
                effective["anger"] + (sensitivity - 0.58),
                "不满与身体敏感叠加时，细小刺激更容易被放大；边界可以更清楚，但不能无依据升级为攻击。",
            ))
        return max(candidates, default=(0.0, ""), key=lambda item: item[0])[1]

    def _render_injection(
        self,
        state: dict[str, Any],
        live_appraisal: dict[str, Any] | None = None,
        body_state: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        candidates = [
            item for item in top_drives(state, 5)
            if float(item["value"]) >= float(self.settings["injection_min_drive"])
        ][: int(self.settings["injection_max_drives"])]
        live_appraisal = live_appraisal if isinstance(live_appraisal, dict) else {}
        live_levels = dict(live_appraisal.get("activationLevels") or {})
        live_keys = sorted(
            dict.fromkeys(
                key for key in live_appraisal.get("activatedDrives", [])
                if key in DRIVE_KEYS
            ),
            key=lambda key: float(live_levels.get(key) or 0),
            reverse=True,
        )[: int(self.settings["injection_max_drives"])]
        intent = pick_intent(state)
        thoughts = sorted(
            [*state["thoughtPool"].get("obsessions", []), *state["thoughtPool"].get("flash", [])],
            key=lambda item: float(item.get("intensity") or 0),
            reverse=True,
        )
        fatigue = float(state.get("fatigue") or 0)
        awareness = state.get("pendingAwareness")
        has_awareness = isinstance(awareness, dict) and bool(awareness.get("residue"))
        offline_afterglow = state.get("pendingOfflineAfterglow")
        has_offline_afterglow = isinstance(offline_afterglow, dict) and bool(
            offline_afterglow.get("afterglow")
        )
        body_state = body_state if isinstance(body_state, dict) else {}
        body_lines = render_body_context(body_state)
        mind_body_bridge = self._mind_body_bridge(state, body_state)
        if (
            not candidates
            and not live_keys
            and not thoughts
            and fatigue < 0.10
            and not has_awareness
            and not has_offline_afterglow
            and not body_lines
            and not mind_body_bridge
            and state.get("consciousness") != "sleeping"
        ):
            return "", {
                "intent": intent.get("key") if intent else None,
                "drives": [],
                "live_drives": [],
                "fatigue": fatigue,
                "consciousness": state.get("consciousness"),
                "body_available": False,
                "reason": "no_significant_signal",
            }
        lines = [
            '<DynamicMindState role="private_inner_state">',
            "以下是角色持续心智与当前身体节律合成的内在背景，不是外部事实，也不是必须说出口的台词。",
            "用它微调注意力、情绪温度、犹豫、主动性和措辞；不得报告数值、系统名称或逐条复述。",
            "它不能覆盖角色设定、长期关系、明确事实、用户边界和当前场景。没有自然表达机会时可以只保留在心里。",
            "本状态只描述角色此刻怎样感受和表达，不改变现实时间线或历史事件顺序。",
        ]
        lines.extend(body_lines)
        if mind_body_bridge:
            lines.append(f"身心合成：{mind_body_bridge}")
        if state.get("consciousness") == "sleeping":
            lines.append("意识节律：刚被本轮互动唤醒，反应可以带有极轻的回神感，但不要机械描写睡醒。")
        if candidates:
            lines.append("当前较显著的潜在倾向：")
            for item in candidates:
                pressure = float(item.get("pressure") or 0)
                activation = float(item.get("activation") or 0)
                if item.get("kind") == "affect":
                    layer = "情绪余波"
                elif activation >= 0.35 and activation >= pressure * 0.75:
                    layer = "当前仍在前景"
                elif pressure >= 0.50:
                    layer = "持续积累"
                else:
                    layer = "背景倾向"
                lines.append(f"- {layer}：{_EXPRESSION[item['key']]}")
        if live_keys:
            lines.append("本轮输入刚刚触发的即时反应方向：")
            lines.extend(
                f"- {_EXPRESSION[key]}"
                for key in live_keys
                if key not in {item["key"] for item in candidates}
            )
            live_note = " ".join(str(live_appraisal.get("guidance") or "").split())[:180]
            if live_note:
                lines.append(f"本轮即时侧重：{live_note}。这是反应倾向，不是新增事实。")
        if self.settings["injection_include_thought"]:
            if thoughts:
                text = str(thoughts[0].get("text") or "").strip()
                if text:
                    lines.append(f"仍在心里盘旋的线索：{text[:180]}。它可能影响联想，但不等于事实。")
        if self.settings["injection_include_fatigue"]:
            if fatigue >= 0.20:
                lines.append("精神负荷偏高，表达可以更短、更安静，但不能以疲惫为由拒绝正常交流。")
            elif fatigue >= 0.10:
                lines.append("精神有轻微负荷，反应节奏可以稍微收敛。")
        if has_awareness:
            lines.append(f"醒来余韵：{str(awareness['residue'])[:220]}。这是梦境残留，不是现实事件。")
            inner = " ".join(str(awareness.get("awareness") or "").split())
            if inner:
                lines.append(f"梦中整理出的内心理解：{inner[:220]}。它可以影响感受，但不能覆盖现实证据。")
        if has_offline_afterglow:
            afterglow = " ".join(str(offline_afterglow.get("afterglow") or "").split())[:260]
            if afterglow:
                lines.append(
                    f"分别期间的内心余韵：{afterglow}。这是独处时形成的短期心理前景，"
                    "不是新增现实经历；只轻微影响本轮的语气、犹豫和靠近方式，不要主动复述全文。"
                )
        lines.append("</DynamicMindState>")
        block = "\n".join(lines)
        limit = int(self.settings["injection_max_chars"])
        if len(block) > limit:
            block = block[: max(0, limit - 28)].rstrip() + "\n</DynamicMindState>"
        return block, {
            "intent": intent.get("key") if intent else None,
            "drives": [item["key"] for item in candidates],
            "live_drives": live_keys,
            "live_source": live_appraisal.get("_source"),
            "fatigue": state.get("fatigue", 0),
            "consciousness": state.get("consciousness"),
            "body_available": bool(body_state.get("available")),
            "body_injected": bool(body_lines or mind_body_bridge),
            "body_phase": body_state.get("phase") if body_state.get("available") else None,
            "body_cycle_day": body_state.get("cycleDay") if body_state.get("available") else None,
            "body_expression_mode": body_state.get("expressionMode"),
            "body_time_band": body_state.get("timeBand"),
            "body_time_label": body_state.get("timeLabel"),
            "body_context": "\n".join([
                *body_lines,
                *([f"身心合成：{mind_body_bridge}"] if mind_body_bridge else []),
            ]),
            "body_chars": len("\n".join([
                *body_lines,
                *([f"身心合成：{mind_body_bridge}"] if mind_body_bridge else []),
            ])),
            "mind_body_bridge": mind_body_bridge,
            "offline_afterglow": bool(has_offline_afterglow),
            "offline_session_id": (
                offline_afterglow.get("session_id") if has_offline_afterglow else None
            ),
        }

    async def apply_offline_afterglow(
        self, key: str, digest: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one bounded house digest without granting it factual authority."""
        session_id = str(digest.get("session_id") or "")
        afterglow = " ".join(str(digest.get("afterglow") or "").split())[:360]
        if not session_id or not afterglow:
            return {"applied": False, "reason": "invalid_digest"}
        result: dict[str, Any] = {"applied": False, "reason": "unknown"}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal result
            ledger = list(current.get("offlineEffectLedger") or [])[-40:]
            if any(str(item.get("session_id") or "") == session_id for item in ledger if isinstance(item, dict)):
                result = {"applied": False, "reason": "already_applied"}
                return current
            applied: list[dict[str, Any]] = []
            for effect in list(digest.get("drive_effects") or [])[:3]:
                if not isinstance(effect, dict):
                    continue
                drive = str(effect.get("key") or "")
                if drive not in DRIVE_KEYS:
                    continue
                delta = _clamp(effect.get("delta"), -0.12, 0.12, 0.0)
                if abs(delta) < 0.01:
                    continue
                before = float(current["driveActivations"].get(drive) or 0)
                current["driveActivations"][drive] = round(_clamp(before + delta, 0, 1, before), 4)
                applied.append({"key": drive, "delta": round(delta, 3)})
            cue = next((
                " ".join(str(item or "").split())[:140]
                for item in list(digest.get("thought_cues") or [])
                if str(item or "").strip()
            ), "")
            if cue and applied:
                add_flash_thought(
                    current["thoughtPool"], applied[0]["key"], cue,
                    min(0.58, 0.40 + abs(float(applied[0]["delta"]))),
                )
            current["pendingOfflineAfterglow"] = {
                "session_id": session_id,
                "digest_id": str(digest.get("id") or ""),
                "afterglow": afterglow,
                "awareness": " ".join(str(digest.get("awareness") or "").split())[:220],
                "applied_drives": applied,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            current["lastOfflineSessionId"] = session_id
            ledger.append({
                "session_id": session_id,
                "digest_id": str(digest.get("id") or ""),
                "at": datetime.now(timezone.utc).isoformat(),
                "applied_drives": applied,
            })
            current["offlineEffectLedger"] = ledger[-40:]
            current["revision"] = int(current.get("revision") or 0) + 1
            result = {"applied": True, "drives": applied, "session_id": session_id}
            return current

        await self.store.update(key, mutate)
        if result.get("applied"):
            self._record("house", "离线周期余韵已进入一次性心理前景", {
                "scope": key, "session_id": session_id,
                "drives": result.get("drives"), "chars": len(afterglow),
            })
        return result

    @staticmethod
    def move_injection_last(req: ProviderRequest) -> None:
        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        mind_parts = [
            part for part in parts
            if str(getattr(part, "text", "") or "").lstrip().startswith("<DynamicMindState")
        ]
        if not mind_parts:
            return
        req.extra_user_content_parts = [
            part for part in parts if part not in mind_parts
        ] + mind_parts

    @staticmethod
    def _as_turn_list(value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item or "") for item in value]
        return [str(value or "")]

    async def _perceive_and_apply(
        self, key: str, umo: str, user_text: Any, assistant_text: Any,
        live_appraisal: dict[str, Any] | None = None,
        request_context: str = "",
    ) -> None:
        # 4.5.3-beta: accepts either a single turn (manual/test callers) or a
        # coalesced batch of turns from the background worker.
        user_turns = self._as_turn_list(user_text)
        assistant_turns = self._as_turn_list(assistant_text)
        joined_user = "\n".join(item for item in user_turns if item)
        joined_assistant = "\n".join(item for item in assistant_turns if item)
        fallback = self._rule_event(joined_user, joined_assistant, live_appraisal)
        event = fallback
        source = "rules"
        diagnostics: dict[str, Any] = {}
        mode = self.settings["perception_mode"]
        should_call = mode == "llm" or (
            mode == "hybrid"
            and (
                len(joined_user) + len(joined_assistant) >= 80
                or any(mark in joined_user for mark in _EMOTIONAL_MARKERS)
            )
        )
        if should_call:
            provider = self._perception_provider(
                "post", self.settings["perception_provider_id"], umo
            )
            if provider is None:
                source = "rules_no_provider"
                diagnostics = {"reason": "provider_missing"}
            elif self._perception_circuit_open(provider, "post"):
                # Skip before building the prompt and character context: an open
                # circuit means the call would be rejected anyway.
                source = "rules_fallback"
                remaining = self._perception_health_payload("post")["circuit_remaining_seconds"]
                diagnostics = {
                    "reason": "circuit_open",
                    "error": f"感知模型连续失败，熔断剩余 {remaining:.1f} 秒",
                    "attempts": 0,
                }
            else:
                try:
                    parsed = await self._llm_perception(
                        umo,
                        user_turns,
                        assistant_turns,
                        live_appraisal=live_appraisal,
                        request_context=request_context,
                    )
                    diagnostics = dict(parsed.get("_llm_meta") or {}) if parsed else {}
                    if parsed and float(parsed.get("confidence") or 0) >= self.settings["perception_min_confidence"]:
                        event = parsed
                        source = "llm"
                    else:
                        source = "rules_low_confidence"
                        diagnostics.update({
                            "reason": "low_confidence",
                            "confidence": float((parsed or {}).get("confidence") or 0),
                            "threshold": self.settings["perception_min_confidence"],
                        })
                except PerceptionCallError as exc:
                    source = "rules_fallback"
                    diagnostics = {
                        "reason": exc.kind,
                        "error": str(exc)[:180],
                        "attempts": exc.attempts,
                    }
                    self._record("perception", "LLM 感知失败，已使用规则", {
                        **diagnostics, "scope": key,
                    })
                except Exception as exc:
                    source = "rules_fallback"
                    diagnostics = {"reason": "unexpected_error", "error": str(exc)[:180]}
                    self._record("perception", "LLM 感知异常，已使用规则", {
                        **diagnostics, "scope": key,
                    })
        declared_activations = set(
            str(key) for key in list(event.get("activatedDrives") or [])
            if str(key) in DRIVE_KEYS
        ) if isinstance(event, dict) and event is not fallback else set()
        if isinstance(event, dict) and event is not fallback:
            event = dict(event)
            fallback_activations = dict(fallback.get("activationLevels") or {})
            fallback_activations.update(dict(event.get("activationLevels") or {}))
            event["activationLevels"] = fallback_activations
            event["activatedDrives"] = list(dict.fromkeys([
                *list(fallback.get("activatedDrives") or []),
                *list(event.get("activatedDrives") or []),
            ]))
            event_satisfaction = dict(event.get("satisfactionLevels") or {})
            fallback_satisfaction = dict(fallback.get("satisfactionLevels") or {})
            for drive_key in ("social", "share"):
                if drive_key in fallback_satisfaction and drive_key not in event_satisfaction:
                    event_satisfaction[drive_key] = fallback_satisfaction[drive_key]
            event["satisfactionLevels"] = event_satisfaction
            event_deltas = dict(event.get("driveDeltas") or {})
            for drive_key, delta_value in dict(fallback.get("driveDeltas") or {}).items():
                event_deltas.setdefault(drive_key, delta_value)
            event["driveDeltas"] = event_deltas
            if not list(event.get("flashThoughts") or []):
                event["flashThoughts"] = fallback.get("flashThoughts") or []
            event["fatigueDelta"] = fallback.get("fatigueDelta", 0.0)
        event, two_stage = self._reconcile_two_stage_event(
            event,
            live_appraisal,
            declared_activations=declared_activations,
        )
        diagnostics["two_stage"] = two_stage
        event = self._sanitize_event(event)
        if not self.settings["perception_store_summary"]:
            event["summary"] = ""
        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            updated, _ = apply_conversation_event(current, event, umo=umo)
            return updated
        state = await self.store.update(key, mutate)
        merged_turns = max(1, len([item for item in user_turns if item]) or 1)
        self._last_perception[key] = {
            "ts": time.time(), "source": source, "event": event,
            "liveAppraisal": copy.deepcopy(live_appraisal) if isinstance(live_appraisal, dict) else None,
            "diagnostics": diagnostics,
            "mergedTurns": merged_turns,
            "batch": copy.deepcopy(self._perception_batch_stats.get(key) or {}),
            "revision": state["revision"],
        }
        self._record("perception", f"对话感知完成 ({source})", {
            "scope": key, "satisfied": event["satisfiedDrives"],
            "satisfaction": event["satisfactionLevels"],
            "activated": event["activatedDrives"],
            "thoughts": len(event["flashThoughts"]),
            "confidence": event.get("confidence", 0),
            "merged_turns": merged_turns,
            "diagnostics": diagnostics,
        })

    @staticmethod
    def _public_live_appraisal(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            key: copy.deepcopy(value.get(key))
            for key in (
                "activatedDrives", "activationLevels", "driveDeltas",
                "flashThoughts", "guidance", "confidence",
            )
            if key in value
        }

    @staticmethod
    def _reconcile_two_stage_event(
        value: Any,
        live_appraisal: dict[str, Any] | None,
        *,
        declared_activations: set[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        event = dict(value) if isinstance(value, dict) else {}
        live = live_appraisal if isinstance(live_appraisal, dict) else {}
        live_keys = [
            str(key) for key in list(live.get("activatedDrives") or [])
            if str(key) in DRIVE_KEYS
        ]
        if not live_keys:
            return event, {
                "available": False, "compared": 0,
                "confirmed": [], "resolved": [], "carried": [],
            }
        declared = set(declared_activations or set())
        activated = [
            str(key) for key in list(event.get("activatedDrives") or [])
            if str(key) in DRIVE_KEYS
        ]
        levels = {
            str(key): float(raw or 0)
            for key, raw in dict(event.get("activationLevels") or {}).items()
            if str(key) in DRIVE_KEYS
        }
        satisfaction = {
            str(key): float(raw or 0)
            for key, raw in dict(event.get("satisfactionLevels") or {}).items()
            if str(key) in DRIVE_KEYS
        }
        live_levels = dict(live.get("activationLevels") or {})
        confirmed: list[str] = []
        resolved: list[str] = []
        carried: list[str] = []
        for key in live_keys:
            if key in declared:
                confirmed.append(key)
                continue
            if satisfaction.get(key, 0.0) >= 0.45:
                activated = [item for item in activated if item != key]
                levels.pop(key, None)
                resolved.append(key)
                continue
            level = max(0.0, min(1.0, float(live_levels.get(key) or 0.5) * 0.78))
            if level >= 0.24:
                if key not in activated:
                    activated.append(key)
                levels[key] = round(level, 3)
                carried.append(key)
        event["activatedDrives"] = list(dict.fromkeys(activated))[:4]
        event["activationLevels"] = {
            key: levels.get(key, 0.5) for key in event["activatedDrives"]
        }
        return event, {
            "available": True,
            "compared": len(live_keys),
            "confirmed": confirmed,
            "resolved": resolved,
            "carried": carried,
            "policy": "reply_reconciled_once",
        }

    def _request_context(
        self,
        now: datetime | None,
        body_state: dict[str, Any] | None,
    ) -> str:
        current = now or datetime.now(timezone.utc)
        zone_name = str(
            getattr(self.plugin, "rp_time_timezone", "")
            or self.settings.get("time_zone")
            or "Asia/Shanghai"
        )
        try:
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            local = current.astimezone(ZoneInfo(zone_name))
        except Exception:
            zone_name = "UTC"
            local = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
        lines = [
            f"当前时间快照：{local.strftime('%Y-%m-%d %H:%M:%S')}（{zone_name}）。",
            "它只属于本轮现实对话；历史日记日期、梦境日期和当前身体节律不得互相替换。",
        ]
        body = body_state if isinstance(body_state, dict) else {}
        if body.get("available"):
            phase = str(body.get("phaseLabel") or body.get("phase") or "").strip()
            band = str(body.get("timeLabel") or body.get("timeBand") or "").strip()
            lines.append(
                f"当前身体节律快照：日期 {body.get('date') or local.date().isoformat()}"
                f"，阶段 {phase or '未标注'}，时段 {band or '未标注'}；仅调节角色此刻感受。"
            )
        return "\n".join(lines)

    async def _appraise_current_input(
        self,
        umo: str,
        user_text: str,
        *,
        now: datetime | None = None,
        body_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = self._rule_appraisal(user_text)
        result = fallback
        source = "rules"
        diagnostics: dict[str, Any] = {}
        mode = self.settings["perception_mode"]
        should_call = mode == "llm" or (
            mode == "hybrid"
            and (
                len(user_text) >= 160
                or any(mark in user_text for mark in _EMOTIONAL_MARKERS)
            )
        )
        if should_call:
            if self._perception_provider(
                "live", self.settings["live_perception_provider_id"], umo
            ) is None:
                source = "rules_no_provider"
                diagnostics = {"reason": "provider_missing"}
            else:
                try:
                    parsed = await self._llm_live_appraisal(
                        umo,
                        user_text,
                        now=now,
                        body_state=body_state,
                    )
                    diagnostics = dict(parsed.get("_llm_meta") or {}) if parsed else {}
                    if parsed and float(parsed.get("confidence") or 0) >= self.settings["perception_min_confidence"]:
                        result = self._merge_live_appraisal(fallback, parsed)
                        source = "llm"
                    else:
                        source = "rules_low_confidence"
                        diagnostics.update({
                            "reason": "low_confidence",
                            "confidence": float((parsed or {}).get("confidence") or 0),
                            "threshold": self.settings["perception_min_confidence"],
                        })
                except PerceptionCallError as exc:
                    source = "rules_fallback"
                    diagnostics = {
                        "reason": exc.kind,
                        "error": str(exc)[:180],
                        "attempts": exc.attempts,
                    }
                    self._record("perception", "本轮即时评估失败，已使用规则", {
                        **diagnostics,
                    })
                except Exception as exc:
                    source = "rules_fallback"
                    diagnostics = {"reason": "unexpected_error", "error": str(exc)[:180]}
                    self._record("perception", "本轮即时评估异常，已使用规则", diagnostics)
        clean = self._sanitize_live_appraisal(result)
        clean["_source"] = source
        clean["_diagnostics"] = diagnostics
        clean["_request_context"] = self._request_context(now, body_state)
        self._record("perception", f"本轮即时评估完成 ({source})", {
            "activated": clean["activatedDrives"],
            "confidence": clean["confidence"],
            "diagnostics": diagnostics,
        })
        return clean

    def _rule_appraisal(self, user_text: str) -> dict[str, Any]:
        text = " ".join(str(user_text or "").split())
        activated: list[str] = []
        levels: dict[str, float] = {}
        deltas: dict[str, float] = {}
        guidance = ""
        if _has_relation_break(text):
            activated.extend(("grieve", "possess", "monitor"))
            levels.update({"grieve": 0.88, "possess": 0.70, "monitor": 0.72})
            deltas["grieve"] = 0.12
            guidance = "先承接关系受损或失去的重量，避免把它当成普通闲聊轻轻带过"
        elif any(token in text for token in ("难过", "伤心", "哭", "委屈", "害怕", "不安", "撑不住")):
            activated.extend(("monitor", "social", "grieve"))
            levels.update({"monitor": 0.68, "social": 0.48, "grieve": 0.62})
            deltas["grieve"] = 0.07
            guidance = "注意对方的脆弱和未说完的情绪，回应应有承接感"
        if any(token in text for token in ("生气", "讨厌", "骗我", "背叛", "滚", "别烦我")):
            activated.extend(("anger", "reflection"))
            levels.update({"anger": 0.80, "reflection": 0.48})
            deltas["anger"] = 0.10
            guidance = guidance or "先识别冲突和边界，不要用无关热情覆盖不满"
        if any(token in text for token in ("想你", "担心你", "等你", "找你", "还好吗")):
            activated.extend(("monitor", "social"))
            levels.update({"monitor": max(levels.get("monitor", 0), 0.66), "social": max(levels.get("social", 0), 0.46)})
            guidance = guidance or "对牵挂与重新连接保持敏感"
        if _has_intimacy_action(text) or "陪着" in text:
            activated.extend(("possess", "crave"))
            levels.update({"possess": max(levels.get("possess", 0), 0.68), "crave": 0.64})
            guidance = guidance or "亲密反应必须服从角色边界、关系阶段和现场语境"
        if "?" in text or "？" in text or any(token in text for token in ("为什么", "怎么", "什么", "哪里")):
            activated.append("curiosity")
            levels["curiosity"] = 0.52
        if any(token in text for token in ("告诉你", "给你看", "分享", "发现了", "想说")):
            activated.extend(("monitor", "curiosity", "social"))
            levels.update({"monitor": max(levels.get("monitor", 0), 0.46), "curiosity": max(levels.get("curiosity", 0), 0.48), "social": max(levels.get("social", 0), 0.42)})
        if not activated and text:
            activated.append("social")
            levels["social"] = 0.32
        ordered = list(dict.fromkeys(key for key in activated if key in DRIVE_KEYS))[:4]
        thought_key = next((key for key in ordered if key in {"grieve", "anger", "monitor", "possess"}), ordered[0] if ordered else "social")
        thoughts = []
        if any(mark in text for mark in _EMOTIONAL_MARKERS):
            thoughts.append({
                "key": thought_key,
                "text": text[:120],
                "intensity": 0.58,
            })
        return {
            "activatedDrives": ordered,
            "activationLevels": {key: round(levels.get(key, 0.50), 3) for key in ordered},
            "driveDeltas": deltas,
            "flashThoughts": thoughts,
            "guidance": guidance,
            "confidence": 0.58,
        }

    def _merge_live_appraisal(
        self,
        fallback: dict[str, Any],
        llm_value: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep explicit rule evidence while letting the LLM supply nuance."""
        rule = self._sanitize_live_appraisal(fallback)
        llm = self._sanitize_live_appraisal(llm_value)
        levels = dict(llm["activationLevels"])
        order = list(llm["activatedDrives"])
        for key in rule["activatedDrives"]:
            rule_level = float(rule["activationLevels"].get(key) or 0.0)
            if not order or rule_level >= 0.60:
                levels[key] = max(float(levels.get(key) or 0.0), rule_level)
                if key not in order:
                    order.append(key)
        order = sorted(
            order,
            key=lambda key: (-float(levels.get(key) or 0.0), order.index(key)),
        )[:4]
        deltas = dict(rule["driveDeltas"])
        for key, amount in llm["driveDeltas"].items():
            if abs(float(amount)) >= abs(float(deltas.get(key) or 0.0)):
                deltas[key] = amount
        thoughts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*llm["flashThoughts"], *rule["flashThoughts"]]:
            marker = (str(item.get("key") or ""), str(item.get("text") or ""))
            if marker in seen:
                continue
            seen.add(marker)
            thoughts.append(item)
            if len(thoughts) >= 2:
                break
        return {
            "activatedDrives": order,
            "activationLevels": {key: round(float(levels.get(key) or 0.5), 3) for key in order},
            "driveDeltas": deltas,
            "flashThoughts": thoughts,
            "guidance": llm["guidance"] or rule["guidance"],
            "confidence": llm["confidence"],
            "_llm_meta": copy.deepcopy(llm_value.get("_llm_meta") or {}),
        }

    def _rule_event(
        self, user_text: str, assistant_text: str,
        live_appraisal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        combined = f"{user_text}\n{assistant_text}"
        satisfied: set[str] = set()
        satisfaction: dict[str, float] = {}
        live_appraisal = live_appraisal if isinstance(live_appraisal, dict) else {}
        deltas: dict[str, float] = dict(live_appraisal.get("driveDeltas") or {})
        thought_key = "social"
        social_relief = _social_satisfaction(user_text, assistant_text)
        if social_relief > 0:
            satisfied.add("social")
            satisfaction["social"] = social_relief
        if any(token in user_text for token in ("想你", "担心", "怎么了", "还好吗", "等你", "找你")):
            satisfied.add("monitor")
            satisfaction["monitor"] = 0.35 if assistant_text.strip() else 0.0
            thought_key = "monitor"
        if "?" in user_text or "？" in user_text or any(token in user_text for token in ("为什么", "怎么", "什么", "哪里")):
            satisfied.add("curiosity")
            satisfaction["curiosity"] = 0.42 if len(assistant_text.strip()) >= 24 else 0.18
            thought_key = "curiosity"
        if _has_intimacy_action(user_text) or "陪着" in user_text:
            satisfied.update({"possess", "crave"})
            reciprocal = _has_intimacy_action(assistant_text) or "陪着" in assistant_text
            satisfaction.update({"possess": 0.52 if reciprocal else 0.30, "crave": 0.55 if reciprocal else 0.28})
            thought_key = "possess"
        share_relief = _share_satisfaction(assistant_text)
        if share_relief > 0:
            satisfied.add("share")
            satisfaction["share"] = share_relief
        if any(token in user_text for token in ("难过", "伤心", "哭")) or _has_relation_break(user_text):
            deltas["grieve"] = 0.10
            thought_key = "grieve"
        if any(token in user_text for token in ("生气", "讨厌", "滚", "骗我", "背叛")):
            deltas["anger"] = 0.10
            thought_key = "anger"
        flash = list(live_appraisal.get("flashThoughts") or [])[:2]
        clean = " ".join(user_text.split())
        if clean and any(mark in clean for mark in _EMOTIONAL_MARKERS) and not flash:
            flash.append({"key": thought_key, "text": clean[:120], "intensity": 0.58})
        return {
            "satisfiedDrives": sorted(satisfied),
            "satisfactionLevels": satisfaction,
            "activatedDrives": list(live_appraisal.get("activatedDrives") or []),
            "activationLevels": dict(live_appraisal.get("activationLevels") or {}),
            "driveDeltas": deltas,
            "flashThoughts": flash,
            "summary": clean[:180],
            "fatigueDelta": _conversation_fatigue_delta(user_text, assistant_text),
            "confidence": 0.55,
        }

    async def _llm_live_appraisal(
        self,
        umo: str,
        user_text: str,
        *,
        now: datetime | None = None,
        body_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        provider = self._perception_provider(
            "live", self.settings["live_perception_provider_id"], umo
        )
        if provider is None:
            return None
        labels = "；".join(f"{key}={DIMENSIONS[key]['label']}" for key in DRIVE_KEYS)
        character_context = self._character_context(
            state_chars=1100,
            profile_chars=700,
            facts_chars=300,
        )
        prompt = f"""你是角色持续心理状态的即时评估器。只判断“这条用户输入会立刻触发角色怎样的内在反应”，不要续写剧情，也不要把用户的情绪直接冒充角色的情绪。
角色背景：
{character_context}

本轮现实基准：
{self._request_context(now, body_state)}

可用驱动力：
{labels}

严格输出 JSON：
{{
  "activatedDrives": ["被本轮输入立即唤起或推到前景的驱动力"],
  "activationLevels": {{"驱动力键": 0.0}},
  "driveDeltas": {{"grieve": 0.0, "anger": 0.0}},
  "flashThoughts": [{{"key":"驱动力键","text":"不超过60字的即时心理线索","intensity":0.0}}],
  "guidance": "不超过80字，只说明本轮回应应注意的心理重心，不写台词",
  "confidence": 0.0
}}

约束：
1. activatedDrives 最多4项；activationLevels 为对应的即时唤起强度 0.0-1.0。普通问候通常只有 social 0.25-0.40；不要把每轮都判成亲密、欲望或重大关系事件。
2. driveDeltas 只写会留下较长余波的变化，主要用于 grieve/anger，范围 -0.20 到 0.20；不能用它代替即时激活。
3. 用户描述自己的感受，不等于角色产生同名情绪；要结合关系语义判断角色会受到什么影响。
4. confidence 表示“判断有多少文本依据”，不是情绪强度；证据清楚但反应轻微时仍应给 0.75 以上。不得编造输入里没有的关系、事实、动作或称呼；真正模糊时 confidence 低于0.65。

本轮用户输入：
{user_text}
"""
        return await self._call_perception_json(
            provider,
            prompt,
            stage="live",
            allow_retry=True,
            retry_transport=False,
        )

    def _sanitize_live_appraisal(self, value: Any) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        raw_levels = source.get("activationLevels")
        levels = raw_levels if isinstance(raw_levels, dict) else {}
        activated = list(dict.fromkeys(
            str(key) for key in source.get("activatedDrives", [])
            if str(key) in DRIVE_KEYS
        ))[:4]
        common = self._sanitize_event({
            "satisfiedDrives": [],
            "driveDeltas": source.get("driveDeltas"),
            "flashThoughts": source.get("flashThoughts"),
            "confidence": source.get("confidence"),
        })
        return {
            "activatedDrives": activated,
            "activationLevels": {
                key: round(_clamp(raw, 0.0, 1.0, 0.50), 3)
                for key, raw in levels.items()
                if key in activated
            } | {
                key: 0.50 for key in activated
                if key not in levels
            },
            "driveDeltas": common["driveDeltas"],
            "flashThoughts": common["flashThoughts"],
            "guidance": " ".join(str(source.get("guidance") or "").split())[:180],
            "confidence": common["confidence"],
        }

    async def _llm_perception(
        self,
        umo: str,
        user_text: Any,
        assistant_text: Any,
        live_appraisal: dict[str, Any] | None = None,
        request_context: str = "",
    ) -> dict[str, Any] | None:
        provider = self._perception_provider(
            "post", self.settings["perception_provider_id"], umo
        )
        if provider is None:
            return None
        user_turns = self._as_turn_list(user_text)
        assistant_turns = self._as_turn_list(assistant_text)
        labels = "\n".join(f"- {key}: {DIMENSIONS[key]['label']}" for key in DRIVE_KEYS)
        if len(user_turns) > 1 or len(assistant_turns) > 1:
            transcript_lines: list[str] = []
            for index in range(max(len(user_turns), len(assistant_turns))):
                spoken = user_turns[index] if index < len(user_turns) else ""
                replied = assistant_turns[index] if index < len(assistant_turns) else ""
                if spoken:
                    transcript_lines.append(f"第{index + 1}轮 用户：{spoken}")
                if replied:
                    transcript_lines.append(f"第{index + 1}轮 角色：{replied}")
            rounds = max(len(user_turns), len(assistant_turns))
            scope_line = (
                f"这是同一段连续互动中的 {rounds} 轮对话，"
                "请把它们当作一次整体互动来判断累积结果，不要逐轮相加。较晚的轮次权重更高。"
            )
            material = scope_line + "\n\n" + "\n".join(transcript_lines)
        else:
            material = f"用户：{user_turns[0] if user_turns else ''}\n角色：{assistant_turns[0] if assistant_turns else ''}"
        live_material = self._public_live_appraisal(live_appraisal)
        request_context = str(
            request_context or (live_appraisal or {}).get("_request_context") or ""
        ).strip()
        prompt = f"""你是角色心理状态的保守事件感知器。只判断实际发生的互动，不续写剧情。
角色背景：
{self._character_context()}

本轮现实基准：
{request_context or "未保留额外时间快照；只按下面实际对话结算。"}

回答前即时评估（暂定观察，不是已经落库的结论）：
{json.dumps(live_material, ensure_ascii=False, separators=(",", ":")) if live_material else "无"}

可用驱动力：
{labels}

输出严格 JSON：
{{
  "satisfiedDrives": ["本轮互动已经明显满足的驱动力"],
  "satisfactionLevels": {{"驱动力键": 0.0}},
  "activatedDrives": ["本轮实际留下的即时激活"],
  "activationLevels": {{"驱动力键": 0.0}},
  "driveDeltas": {{"grieve": 0.0, "anger": 0.0}},
  "flashThoughts": [{{"key":"驱动力键","text":"不超过80字、只写本轮留下的心理线索","intensity":0.0}}],
  "summary": "不超过100字的客观互动摘要",
  "confidence": 0.0
}}

约束：
1. satisfactionLevels 表示实际满足程度 0.0-1.0。普通回复对 social 通常只有 0.10-0.40；只有充分交流和真正被承接时才可更高。冷淡、拒绝或冲突不能算充分满足。
2. satisfiedDrives 只列 satisfactionLevels 大于零的键。share 只有角色实际分享了自己的发现、经历或内心内容才算满足，不能因为用户在分享就满足角色的 share。
3. activatedDrives/activationLevels 表示本轮被推到前景但未必满足的倾向。不要把“产生欲望”误写成满足。
4. driveDeltas 主要用于 grieve/anger 的持续余波，范围 -0.20 到 0.20；没有依据就留空。
5. flashThoughts 最多2条，不得编造对话中没有的事实。
6. confidence 表示“判断有多少对话依据”，不是满足或激活强度；证据清楚但变化轻微时仍应给 0.75 以上，真正模糊时才低于 0.65。
7. 必须用角色的实际回复复核即时评估：若某驱动力已被充分满足，可降低或移除其即时激活；若回复后仍未解决，才保留。不能把前后两次判断重复累计。

{material}
"""
        return await self._call_perception_json(
            provider,
            prompt,
            stage="post",
            allow_retry=True,
        )

    async def _call_perception_json(
        self,
        provider: Any,
        prompt: str,
        *,
        stage: str,
        allow_retry: bool,
        retry_transport: bool = True,
    ) -> dict[str, Any]:
        if self._perception_circuit_open(provider, stage):
            remaining = self._perception_health_payload(stage)["circuit_remaining_seconds"]
            raise PerceptionCallError(
                "circuit_open",
                f"感知模型连续失败，熔断剩余 {remaining:.1f} 秒",
                attempts=0,
            )
        timeout_key = (
            "live_perception_timeout_seconds"
            if stage == "live"
            else "post_perception_timeout_seconds"
        )
        timeout_default = 10 if stage == "live" else 60
        timeout = max(0.01, float(self.settings.get(timeout_key) or timeout_default))
        deadline = time.monotonic() + timeout
        max_attempts = 2 if allow_retry else 1
        last_kind = "unknown"
        last_error = "感知模型未返回有效结果"
        system_prompt = (
            "你是角色心理状态的结构化感知器。只输出一个合法 JSON 对象；"
            "不要输出 Markdown、代码围栏、推理过程或额外说明。"
        )
        attempts_made = 0
        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_kind = "timeout"
                last_error = f"总超时 {timeout:.1f} 秒"
                break
            try:
                attempt_prompt = prompt
                if attempt > 1:
                    attempt_prompt += (
                        "\n\n上一次输出未通过结构校验。重新独立判断，只输出完整 JSON；"
                        "不要解释，不要省略 confidence。"
                    )
                response = await self._managed_text_chat(
                    provider,
                    prompt=attempt_prompt,
                    system_prompt=system_prompt,
                    timeout=remaining,
                    label=f"xinchao_{stage}",
                )
            except asyncio.TimeoutError:
                last_kind = "timeout"
                last_error = f"调用超过 {timeout:.1f} 秒"
                break
            except asyncio.CancelledError:
                raise
            except LLMCircuitOpenError as exc:
                last_kind = "circuit_open"
                last_error = str(exc)[:240]
                break
            except DirectLLMEmptyFinalError as exc:
                # A reasoning-only completion already consumed its output
                # allowance; an identical retry in the remaining time is wasteful.
                last_kind = "empty_final"
                last_error = str(exc)[:240]
                break
            except Exception as exc:
                last_kind = "provider_error"
                last_error = str(exc)[:240] or exc.__class__.__name__
                if retry_transport and attempt < max_attempts and deadline - time.monotonic() > 0.05:
                    continue
                break
            raw_text = self._perception_response_text(response)[:32000]
            parsed = self._parse_json(raw_text)
            if parsed is not None:
                normalized = self._normalize_perception_payload(parsed)
                required = (
                    {"activatedDrives", "activationLevels", "driveDeltas", "confidence"}
                    if stage == "live"
                    else {
                        "satisfiedDrives", "satisfactionLevels", "activatedDrives",
                        "activationLevels", "driveDeltas", "confidence",
                    }
                )
                present = (required - {"confidence"}).intersection(normalized)
                # Immediate appraisal can still be useful when a model supplies
                # one grounded signal plus confidence. Missing neutral containers
                # are filled below; the background settlement remains stricter.
                minimum_fields = 1 if stage == "live" else 3
                if len(present) >= minimum_fields and "confidence" in normalized:
                    for key in required - {"confidence"}:
                        if key not in normalized:
                            normalized[key] = {} if key.endswith("Levels") or key == "driveDeltas" else []
                    self._mark_perception_success(provider, stage)
                    normalized["_llm_meta"] = {
                        "attempts": attempt,
                        "provider": self._provider_name(provider),
                        "stage": stage,
                        "timeout": timeout,
                        "recovered": attempt > 1,
                        "recovered_from": last_kind if attempt > 1 else "",
                    }
                    return normalized
                last_kind = "schema_error"
                last_error = f"JSON 感知字段不足（需要至少 {minimum_fields} 项）或缺少 confidence"
            else:
                last_kind = "parse_error" if raw_text else "empty_response"
                last_error = "返回内容不是可解析的 JSON" if raw_text else "模型返回空内容"
            if attempt >= max_attempts or deadline - time.monotonic() <= 0.05:
                break
        self._mark_perception_failure(provider, stage, last_kind, last_error)
        raise PerceptionCallError(last_kind, last_error, attempts=attempts_made)

    @staticmethod
    def _perception_response_text(response: Any) -> str:
        if isinstance(response, str):
            return response.strip()
        if isinstance(response, dict):
            for key in ("completion_text", "output_text", "text", "content"):
                value = response.get(key)
                if value:
                    return str(value).strip()
            return ""
        for name in ("completion_text", "output_text", "text", "content"):
            value = getattr(response, name, None)
            if value:
                return str(value).strip()
        return ""

    def _sanitize_event(self, value: Any) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        raw_satisfaction = source.get("satisfactionLevels")
        satisfaction_source = raw_satisfaction if isinstance(raw_satisfaction, dict) else {}
        raw_activation = source.get("activationLevels")
        activation_source = raw_activation if isinstance(raw_activation, dict) else {}
        raw_deltas = source.get("driveDeltas")
        delta_source = raw_deltas if isinstance(raw_deltas, dict) else {}
        satisfied = sorted({str(key) for key in source.get("satisfiedDrives", []) if str(key) in DRIVE_KEYS})
        satisfaction = {
            str(key): round(_clamp(raw, 0.0, 1.0, 0.0), 3)
            for key, raw in satisfaction_source.items()
            if str(key) in DRIVE_KEYS and _clamp(raw, 0.0, 1.0, 0.0) >= 0.01
        }
        for key in satisfied:
            satisfaction.setdefault(key, 0.45)
        satisfied = sorted(satisfaction)
        activated = list(dict.fromkeys(
            str(key) for key in source.get("activatedDrives", [])
            if str(key) in DRIVE_KEYS
        ))[:4]
        activation = {
            str(key): round(_clamp(raw, 0.0, 1.0, 0.0), 3)
            for key, raw in activation_source.items()
            if str(key) in DRIVE_KEYS and _clamp(raw, 0.0, 1.0, 0.0) >= 0.01
        }
        for key in activated:
            activation.setdefault(key, 0.55)
        activated = list(dict.fromkeys([*activated, *activation.keys()]))[:4]
        activation = {key: activation[key] for key in activated}
        deltas = {}
        for key, raw in delta_source.items():
            if key not in DRIVE_KEYS:
                continue
            amount = _clamp(raw, -0.20, 0.20, 0.0)
            if abs(amount) >= 0.005:
                deltas[key] = round(amount, 4)
        thoughts = []
        for item in list(source.get("flashThoughts") or [])[:2]:
            if not isinstance(item, dict) or item.get("key") not in DRIVE_KEYS:
                continue
            text = " ".join(str(item.get("text") or "").split())[:120]
            if text:
                thoughts.append({
                    "key": item["key"], "text": text,
                    "intensity": round(_clamp(item.get("intensity"), 0.25, 0.85, 0.55), 3),
                })
        return {
            "satisfiedDrives": satisfied,
            "satisfactionLevels": satisfaction,
            "activatedDrives": activated,
            "activationLevels": activation,
            "driveDeltas": deltas,
            "flashThoughts": thoughts,
            "summary": " ".join(str(source.get("summary") or "").split())[:180],
            "fatigueDelta": round(_clamp(source.get("fatigueDelta"), -0.08, 0.08, 0.0), 4),
            "confidence": round(_clamp(source.get("confidence"), 0, 1, 0), 3),
        }

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        source = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I).strip()
        candidates = [
            match.group(1).strip()
            for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", source, re.I)
        ]
        candidates.append(source)
        decoder = json.JSONDecoder(strict=False)
        for candidate in candidates:
            starts = [index for index, char in enumerate(candidate) if char == "{"]
            if not starts and candidate:
                starts = [0]
            for start in starts:
                fragment = candidate[start:].strip()
                if not fragment:
                    continue
                try:
                    parsed, _ = decoder.raw_decode(fragment)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                repaired = re.sub(r",\s*([}\]])", r"\1", fragment)
                try:
                    parsed, _ = decoder.raw_decode(repaired)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                try:
                    parsed = ast.literal_eval(repaired)
                    if isinstance(parsed, dict):
                        return parsed
                except (SyntaxError, ValueError):
                    pass
        return None

    @staticmethod
    def _normalize_perception_payload(value: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "satisfiedDrives", "satisfactionLevels", "activatedDrives",
            "activationLevels", "driveDeltas", "flashThoughts", "guidance",
            "summary", "confidence",
        }
        source = dict(value)
        if not expected.intersection(source):
            for wrapper in ("result", "data", "appraisal", "event", "output"):
                nested = source.get(wrapper)
                if isinstance(nested, dict):
                    source = dict(nested)
                    break
        aliases = {
            "satisfied_drives": "satisfiedDrives",
            "satisfaction_levels": "satisfactionLevels",
            "activated_drives": "activatedDrives",
            "activation_levels": "activationLevels",
            "drive_deltas": "driveDeltas",
            "flash_thoughts": "flashThoughts",
            "response_guidance": "guidance",
            "thoughts": "flashThoughts",
            "confidence_score": "confidence",
            "certainty": "confidence",
        }
        for old, new in aliases.items():
            if new not in source and old in source:
                source[new] = source[old]
        labels = {
            str(meta.get("label") or "").strip().lower(): key
            for key, meta in DIMENSIONS.items()
            if str(meta.get("label") or "").strip()
        }
        labels.update({
            "亲密": "possess", "占有": "possess", "靠近": "possess",
            "牵挂": "monitor", "关心": "monitor", "近况": "monitor",
            "依恋": "crave", "身体接近": "crave",
            "分享": "share", "表达": "share",
            "欲望": "libido", "身体欲望": "libido",
            "好奇": "curiosity", "探索": "curiosity",
            "无聊": "boredom",
            "聊天": "social", "社交": "social",
            "责任": "duty", "责任感": "duty",
            "反思": "reflection", "沉淀": "reflection",
            "悲伤": "grieve", "难过": "grieve", "失落": "grieve",
            "愤怒": "anger", "生气": "anger", "不满": "anger",
        })

        def drive_key(raw: Any) -> str:
            clean = str(raw or "").strip()
            lowered = clean.lower()
            return lowered if lowered in DRIVE_KEYS else labels.get(lowered, clean)

        for name in ("satisfiedDrives", "activatedDrives"):
            raw = source.get(name, [])
            if isinstance(raw, str):
                raw = re.split(r"[,，、\s]+", raw)
            source[name] = [drive_key(item) for item in list(raw or [])]
        for name in ("satisfactionLevels", "activationLevels", "driveDeltas"):
            if name not in source:
                continue
            raw = source.get(name)
            if isinstance(raw, dict):
                source[name] = {drive_key(key): amount for key, amount in raw.items()}
            else:
                source[name] = {}
        thoughts = []
        for item in list(source.get("flashThoughts") or []):
            if isinstance(item, str):
                thoughts.append({"key": "social", "text": item, "intensity": 0.5})
                continue
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["key"] = drive_key(
                item.get("key") or item.get("drive") or item.get("dimension")
            )
            normalized["text"] = item.get("text") or item.get("content") or item.get("thought") or ""
            normalized["intensity"] = item.get("intensity", item.get("score", 0.5))
            thoughts.append(normalized)
        source["flashThoughts"] = thoughts
        confidence = source.get("confidence")
        if isinstance(confidence, str):
            clean = confidence.strip().rstrip("%")
            try:
                numeric = float(clean)
                source["confidence"] = numeric / 100 if "%" in confidence or numeric > 1 else numeric
            except ValueError:
                source["confidence"] = 0.0
        return source

    def _provider(self, provider_id: str, umo: str):
        resolver = getattr(self.plugin, "_resolve_chat_provider", None)
        if callable(resolver):
            return resolver(provider_id, umo)
        try:
            if provider_id:
                return self.plugin.context.get_provider_by_id(provider_id)
            return self.plugin.context.get_using_provider(umo)
        except Exception:
            return None

    def _perception_provider(self, stage: str, provider_id: str, umo: str):
        external = getattr(self.plugin, "_external_models", None)
        if external is not None and external.enabled:
            return self._provider(provider_id, umo)
        mode = str(self.settings.get("perception_api_mode") or "off")
        direct = mode == "all" or (mode == "post" and stage == "post")
        if direct:
            return self._direct_perception if self._direct_perception.configured else None
        return self._provider(provider_id, umo)

    async def _managed_text_chat(
        self,
        provider: Any,
        *,
        prompt: str,
        system_prompt: str = "",
        timeout: float,
        label: str,
    ) -> Any:
        caller = getattr(self.plugin, "_plugin_llm_text_chat", None)
        if callable(caller):
            return await caller(
                provider,
                prompt=prompt,
                contexts=[],
                system_prompt=system_prompt,
                timeout=timeout,
                label=label,
                optional=True,
            )
        # Compatibility path for isolated controller tests and older hosts.
        return await asyncio.wait_for(
            provider.text_chat(prompt=prompt, contexts=[], system_prompt=system_prompt),
            timeout=timeout,
        )

    @staticmethod
    def _provider_name(provider: Any) -> str:
        try:
            meta = provider.meta()
            return str(getattr(meta, "id", "") or provider.__class__.__name__)
        except Exception:
            return provider.__class__.__name__

    def _perception_health_payload(self, stage: str = "post") -> dict[str, Any]:
        stage = "live" if stage == "live" else "post"
        health = copy.deepcopy(self._perception_health[stage])
        remaining = max(0.0, float(health.get("circuit_until") or 0.0) - time.monotonic())
        health["circuit_remaining_seconds"] = round(remaining, 1)
        if remaining > 0:
            health["state"] = "circuit_open"
        elif (
            int(health.get("consecutive_failures") or 0) > 0
            or int(health.get("consecutive_soft_failures") or 0) > 0
        ):
            health["state"] = "degraded"
        elif float(health.get("last_success_at") or 0) > 0:
            health["state"] = "healthy"
        else:
            health["state"] = "idle"
        health.pop("circuit_until", None)
        health["stage"] = stage
        return health

    def _perception_health_by_stage_payload(self) -> dict[str, dict[str, Any]]:
        return {
            "live": self._perception_health_payload("live"),
            "post": self._perception_health_payload("post"),
        }

    def _mark_perception_success(self, provider: Any, stage: str) -> None:
        health = self._perception_health["live" if stage == "live" else "post"]
        health.update({
            "provider": self._provider_name(provider),
            "consecutive_failures": 0,
            "consecutive_soft_failures": 0,
            "circuit_until": 0.0,
            "circuit_backoff_seconds": 0.0,
            "last_error": "",
            "last_error_kind": "",
            "last_success_at": time.time(),
        })

    def _mark_perception_failure(
        self, provider: Any, stage: str, kind: str, error: str,
    ) -> None:
        health = self._perception_health["live" if stage == "live" else "post"]
        hard_failure = kind in {"timeout", "provider_error", "empty_response"}
        failures = int(health.get("consecutive_failures") or 0) + (1 if hard_failure else 0)
        soft_failures = (
            0 if hard_failure
            else int(health.get("consecutive_soft_failures") or 0) + 1
        )
        health.update({
            "provider": self._provider_name(provider),
            "consecutive_failures": failures,
            "consecutive_soft_failures": soft_failures,
            "last_error": str(error or kind)[:240],
            "last_error_kind": kind,
            "last_failure_at": time.time(),
        })
        if hard_failure and failures >= _PERCEPTION_CIRCUIT_FAILURES:
            # A provider that is still broken when the circuit reopens should not
            # cost another full timeout every three minutes, so each reopen-then-fail
            # doubles the cooldown up to the ceiling.
            previous = float(health.get("circuit_backoff_seconds") or 0.0)
            backoff = _PERCEPTION_CIRCUIT_SECONDS if previous <= 0 else previous * 2
            backoff = min(backoff, _PERCEPTION_CIRCUIT_MAX_SECONDS)
            health["circuit_backoff_seconds"] = backoff
            health["circuit_until"] = time.monotonic() + backoff

    def _perception_circuit_open(self, provider: Any, stage: str) -> bool:
        health = self._perception_health["live" if stage == "live" else "post"]
        provider_name = self._provider_name(provider)
        current_name = str(health.get("provider") or "")
        if current_name and current_name != provider_name:
            health.update({
                "provider": provider_name,
                "consecutive_failures": 0,
                "consecutive_soft_failures": 0,
                "circuit_until": 0.0,
                "circuit_backoff_seconds": 0.0,
                "last_error": "",
                "last_error_kind": "",
            })
            return False
        return float(health.get("circuit_until") or 0.0) > time.monotonic()

    def _character_context(
        self,
        *,
        state_chars: int = 1400,
        profile_chars: int = 1400,
        facts_chars: int = 600,
    ) -> str:
        name = str(getattr(self.plugin, "character_name", "") or "当前角色").strip()
        lines = [f"角色名：{name}"]
        state_reader = getattr(self.plugin, "_semantic_state_status", None)
        if callable(state_reader):
            try:
                status = state_reader()
                state = status.get("state") if isinstance(status, dict) else {}
                rendered = " ".join(
                    str((state or {}).get("rendered_text") or "").split()
                )[:max(0, int(state_chars))]
                if rendered:
                    lines.append(
                        "当前滚动人格状态（已融合的现在，不是本轮事件证据）：" + rendered
                    )
            except Exception:
                pass
        reader = getattr(self.plugin, "_affiliate_profile_status", None)
        if callable(reader):
            try:
                status = reader()
                if status.get("connected"):
                    profile = " ".join(str(status.get("profile") or "").split())[:max(0, int(profile_chars))]
                    facts = " ".join(str(status.get("profile_facts") or "").split())[:max(0, int(facts_chars))]
                    if profile:
                        lines.append("长期人格画像：" + profile)
                    if facts:
                        lines.append("稳定事实锚点：" + facts)
            except Exception:
                pass
        return "\n".join(lines)

    async def _create_dream(self, key: str, state: dict[str, Any]) -> dict[str, Any]:
        sleep_token = (state.get("sleepStartedAt"), state.get("lastUmo"))
        provider = self._provider(self.settings["dream_provider_id"], state.get("lastUmo", ""))
        character_context = self._character_context()
        events = "\n".join(f"- {item.get('summary', '')}" for item in state.get("recentEvents", [])[-8:])
        memory_material = self._memory_material()
        drives = "；".join(f"{item['label']} {item['value']:.2f}" for item in top_drives(state, 4))
        thoughts = "；".join(
            str(item.get("text") or "") for item in [
                *state["thoughtPool"].get("obsessions", []),
                *state["thoughtPool"].get("flash", []),
            ][-5:]
        )
        body_state = self._body_state(scope_key=key)
        body_tendencies = [
            str(item or "").strip()
            for item in list(body_state.get("tendencies") or [])
            if str(item or "").strip()
        ]
        if body_state.get("expressionMode") == "significant":
            body_material_parts = body_tendencies[:3]
        else:
            body_material_parts = [
                str(body_state.get("phaseExperience") or "").strip(),
                str(body_state.get("timeExperience") or "").strip(),
                *body_tendencies[:2],
            ]
        body_material = "\n".join(
            f"- {item}" for item in body_material_parts if item
        )
        house_cues: list[dict[str, Any]] = []
        house = getattr(self.plugin, "_house", None)
        if house is not None:
            try:
                house_cues = await house.dream_cues(key, 2)
            except Exception as exc:
                self._record("house", "梦境读取小屋线索失败，已继续原梦境流程", {
                    "scope": key, "error": str(exc)[:160],
                })
        house_material = "\n".join(
            f"- [{item.get('type') or 'inner'}] {str(item.get('summary') or '')[:180]}"
            for item in house_cues
            if str(item.get("summary") or "").strip()
        )
        dream_text = ""
        residue = ""
        awareness = ""
        source = "deterministic"
        if provider is not None:
            prompt = f"""为下面这个正在睡眠的角色生成一次内在梦境结算。材料只用于联想，不能把梦写成现实。
{character_context}
近期互动：
{events or "没有足够材料"}
长期记忆片段：
{memory_material or "没有读取长期记忆"}
当前身体底色：
{body_material or "当前没有需要进入梦境的显著身体体验"}
同一离线周期里的清醒独处线索（最多两条，只可化为梦的意象，不要重复原文）：
{house_material or "无"}
主要驱动力：{drives}
盘旋念头：{thoughts or "无"}

严格输出 JSON：
{{"dream":"有文学质感但克制的梦境，不超过600字","residue":"醒来后残留的情绪或意象，不超过160字","awareness":"梦中隐约整理出的内心理解，不超过200字"}}
记忆中的内容是过去材料，身体底色只是当下感受，不是剧情事实。梦可以重组意象但不能把梦写成现实，也不能新增现实事实。不要写系统、周期阶段、数值或分析报告。
"""
            try:
                response = await self._managed_text_chat(
                    provider,
                    prompt=prompt,
                    system_prompt="",
                    timeout=float(self.settings["dream_timeout_seconds"]),
                    label="xinchao_dream",
                )
                parsed = self._parse_json(getattr(response, "completion_text", "") or "") or {}
                dream_text = " ".join(str(parsed.get("dream") or "").split())[:1200]
                residue = " ".join(str(parsed.get("residue") or "").split())[:300]
                awareness = " ".join(str(parsed.get("awareness") or "").split())[:400]
                source = "llm"
            except Exception as exc:
                self._record("dream", "梦境 LLM 失败，使用确定性余韵", {"error": str(exc)[:180]})
        if not residue:
            primary = top_drives(state, 1)[0]
            residue = f"梦里留下了一点关于{primary['label']}的模糊余韵。"
        accepted = False
        def commit_dream(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal accepted
            # A slow result belongs to its sleep episode, not the next conversation.
            if (current.get("consciousness") != "sleeping"
                    or (current.get("sleepStartedAt"), current.get("lastUmo")) != sleep_token
                    or not dream_allowed(current, datetime.now(timezone.utc),
                        self.settings["dream_min_interval_hours"], self.settings["dream_max_per_day"])):
                return current
            accepted = True
            return record_dream(current,
                residue=residue,
                awareness=awareness,
                dream_text=dream_text,
                source=source,
                used_memory=bool(memory_material),
            )
        updated = await self.store.update(key, commit_dream)
        if not accepted:
            self._record("dream", "旧睡眠周期结果已忽略", {"scope": key, "reason": "stale_sleep_result"})
            return updated
        self._record("dream", "生成一次梦境余韵", {
            "scope": key,
            "source": source,
            "used_memory": bool(memory_material),
            "residue": residue[:100],
            "house_cues": len(house_cues),
        })
        if house_cues and house is not None:
            try:
                dream_id = str((updated.get("recentDreams") or [{}])[-1].get("id") or "")
                if dream_id:
                    args = (
                        [str(item.get("id") or "") for item in house_cues if item.get("id")],
                        "dream", dream_id,
                        hashlib.sha256(residue.encode("utf-8")).hexdigest(),
                    )
                    runner = getattr(self.plugin, "_run_background_work", None)
                    if callable(runner):
                        await runner(house.store.consume_many, *args)
                    else:
                        await asyncio.to_thread(house.store.consume_many, *args)
            except Exception:
                pass
        return updated

    def _memory_material(self, max_items: int | None = None, mode: str = "dream") -> str:
        if mode == "dream" and not self.settings.get("dream_memory_enable", True):
            return ""
        vec = getattr(self.plugin, "_vec", None)
        if vec is None:
            return ""
        try:
            rows = vec.list_memories(limit=80)
        except Exception:
            return ""
        ranked = sorted(
            rows,
            key=lambda item: (
                int(item.get("importance") or 0),
                bool(item.get("manual")),
                float(item.get("event_ts") or item.get("source_created_ts") or item.get("created_ts") or 0),
            ),
            reverse=True,
        )
        limit = max(1, int(max_items or self.settings["dream_memory_items"]))
        if mode == "daytime" and len(ranked) > limit:
            # A stable hourly shuffle avoids surfacing only the same highest-rated anchors.
            seed = int(time.time() // 3600)
            pool = ranked[: min(40, len(ranked))]
            random.Random(seed).shuffle(pool)
            ranked = pool
        lines = []
        for item in ranked:
            preview = " ".join(str(item.get("preview") or "").split())
            if not preview:
                continue
            when = str(item.get("ts_text") or item.get("occurred_at") or "过去")
            kind = str(item.get("memory_type") or "memory")
            lines.append(f"- [{when} / {kind}] {preview[:220]}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    def _daytime_memory_material(
        self,
        state: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        vec = getattr(self.plugin, "_vec", None)
        if vec is None:
            return "", []
        try:
            rows = vec.list_memories(limit=80)
        except Exception:
            return "", []
        current = now or datetime.now(timezone.utc)
        blocked = recent_daytime_memory_keys(
            state,
            current,
            self.settings["daytime_memory_cooldown_hours"],
        )
        ranked = sorted(
            rows,
            key=lambda item: (
                int(item.get("importance") or 0),
                bool(item.get("manual")),
                float(item.get("event_ts") or item.get("source_created_ts") or item.get("created_ts") or 0),
            ),
            reverse=True,
        )
        eligible: list[dict[str, Any]] = []
        for item in ranked:
            memory_key = str(item.get("memo_name") or "").strip()
            preview = " ".join(str(item.get("preview") or "").split())
            if not memory_key or not preview or memory_key in blocked:
                continue
            eligible.append(item)
        if not eligible:
            return "", []

        pool = eligible[: min(40, len(eligible))]
        seed = f"{int(current.timestamp() // 3600)}|{getattr(self.plugin, 'character_name', '')}"
        random.Random(seed).shuffle(pool)
        limit = max(1, int(self.settings["daytime_memory_items"]))
        selected = pool[:limit]
        sources: list[dict[str, str]] = []
        lines: list[str] = []
        for index, item in enumerate(selected, start=1):
            memory_key = str(item.get("memo_name") or "").strip()
            preview = " ".join(str(item.get("preview") or "").split())
            when = str(item.get("ts_text") or item.get("occurred_at") or "过去")
            kind = str(item.get("memory_type") or "memory")
            sources.append({"key": memory_key, "preview": preview[:220]})
            lines.append(f"{index}. [{when} / {kind}] {preview[:220]}")
        return "\n".join(lines), sources

    async def _send_active_message(
        self, key: str, state: dict[str, Any], kind: str,
    ) -> tuple[dict[str, Any], bool]:
        lock = self._active_send_locks.setdefault(key, asyncio.Lock())
        async with lock:
            latest = await self.store.read(key)
            return await self._send_active_message_unlocked(key, latest, kind)

    async def _send_active_message_unlocked(
        self, key: str, state: dict[str, Any], kind: str,
    ) -> tuple[dict[str, Any], bool]:
        provider = self._provider(self.settings["proactive_provider_id"], state.get("lastUmo", ""))
        if provider is None:
            return state, False
        intent = pick_intent(state)
        character_context = self._character_context()
        recent_items = recent_bark_history(state, 8)
        recent = [str(item.get("message") or "") for item in recent_items]
        events = "\n".join(f"- {item.get('summary', '')}" for item in state.get("recentEvents", [])[-5:])
        drive_material = "\n".join(
            f"- {item['label']}：综合 {item['value']:.2f}，长期积累 {item['pressure']:.2f}，当前激活 {item['activation']:.2f}"
            for item in top_drives(state, 4)
        )
        thought_material = "\n".join(
            f"- {str(item.get('text') or '')[:160]}"
            for item in sorted(
                [
                    *state.get("thoughtPool", {}).get("obsessions", []),
                    *state.get("thoughtPool", {}).get("flash", []),
                ],
                key=lambda item: float(item.get("intensity") or 0),
                reverse=True,
            )[:3]
            if str(item.get("text") or "").strip()
        )
        active_body_state = self._body_state(scope_key=key)
        active_body = "\n".join(
            f"- {str(item).strip()}"
            for item in list(active_body_state.get("tendencies") or [])[:3]
            if str(item).strip()
        )
        mind_body_bridge = self._mind_body_bridge(state, active_body_state)
        daytime_sources: list[dict[str, str]] = []
        daytime_material = ""
        if kind == "daytime_emergence":
            daytime_material, daytime_sources = self._daytime_memory_material(state)
        if kind == "daytime_emergence" and not daytime_material.strip():
            self._record("daytime", "白天记忆浮现跳过：没有未冷却的可用记忆", {
                "scope": key,
                "cooldown_hours": self.settings["daytime_memory_cooldown_hours"],
            })
            return state, False
        rejected = ""
        message = ""
        selected_daytime_keys: list[str] = []
        attempts = int(self.settings["proactive_duplicate_attempts"])
        for attempt in range(1, attempts + 1):
            if kind == "dream":
                dream = state.get("recentDreams", [])[-1] if state.get("recentDreams") else {}
                prompt = f"""你是角色半梦半醒的潜意识。把以下梦境余韵变成一条自然的主动私聊消息。
{character_context}
梦境：{str(dream.get('dream') or '')[:900]}
余韵：{str(dream.get('residue') or '')[:300]}
醒后理解：{str(dream.get('awareness') or '')[:400]}
最近已发送的跨类型主动消息：
{chr(10).join(recent) or "无"}
{f"上一候选因措辞近似被拒绝：{rejected}。保留真实主题，但换角度、句式和具体表达。" if rejected else ""}

只输出一条第一人称私聊消息，一至两句，不超过80字。不要说“梦境系统”、驱动力或数值，不要把梦写成现实。没有自然表达时输出 SKIP。
"""
            elif kind == "daytime_emergence":
                prompt = f"""你是角色白天持续运行的后台心智。判断一小段过去记忆是否值得自然浮现，并变成一条主动私聊消息。
{character_context}
候选过去记忆（只能选择其中一项）：
{daytime_material}
此刻内在方向（只用于判断哪段记忆更可能自然浮现）：
{drive_material}
仍在盘旋的心理线索：
{thought_material or "无"}
最近已发送的跨类型主动消息：
{chr(10).join(recent) or "无"}
{f"上一候选因措辞近似被拒绝：{rejected}。保留真实主题，但换角度、句式和具体表达。" if rejected else ""}

有具体画面、牵挂、细节或没说完的话才发送。第一人称、普通口语，一至两句，不超过80字；不得把历史记忆说成刚刚发生，不得编造当前事实。
内在方向只能影响选择和语气，不能被直接说成理由；候选记忆与当前心理没有自然联系时必须 SKIP。
严格输出 JSON：{{"memory_index":候选编号,"message":"可直接发送的消息"}}。没有自然理由时输出 {{"memory_index":0,"message":"SKIP"}}。
"""
            else:
                prompt = f"""你要根据角色持续心境决定一条自然的主动私聊消息。
{character_context}
当前最强内在方向：{intent.get('label') if intent else '不明确'}
分层心理状态：
{drive_material}
仍在盘旋的心理线索：
{thought_material or "无"}
当前身体表达底色：
{active_body or "没有显著身体倾向"}
身心合成：{mind_body_bridge or "没有需要特别协调的身心冲突"}
近期互动线索：
{events or "无"}
近期跨类型主动消息（不得复用措辞）：
{chr(10).join(recent) or "无"}
{f"上一候选因措辞近似被拒绝：{rejected}。保留真实主题，但换角度、句式和具体表达。" if rejected else ""}

只输出一条可以直接发送的角色消息，1至3句。长期积累决定潜在主题，当前激活决定眼下紧迫度；身体底色只能改变节奏和表达方式，不能制造情绪、关系或事实。不要提系统、驱动力、数值、身体周期或“突然想起你”等固定模板。
没有足够自然理由时输出：SKIP
"""
            try:
                response = await self._managed_text_chat(
                    provider,
                    prompt=prompt,
                    system_prompt="",
                    timeout=float(self.settings["proactive_timeout_seconds"]),
                    label=f"xinchao_{kind}",
                )
                raw_message = str(getattr(response, "completion_text", "") or "").strip()
                if kind == "daytime_emergence":
                    parsed = self._parse_json(raw_message) or {}
                    parsed_message = parsed.get("message") if isinstance(parsed, dict) else None
                    if parsed_message is not None:
                        message = " ".join(str(parsed_message or "").strip().split())
                        try:
                            selected_index = int(parsed.get("memory_index") or 0)
                        except (TypeError, ValueError):
                            selected_index = 0
                        if 1 <= selected_index <= len(daytime_sources):
                            selected_daytime_keys = [daytime_sources[selected_index - 1]["key"]]
                        else:
                            selected_daytime_keys = [
                                item["key"] for item in daytime_sources
                            ]
                    else:
                        message = " ".join(raw_message.split())
                        selected_daytime_keys = [
                            item["key"] for item in daytime_sources
                        ]
                else:
                    message = " ".join(raw_message.split())
            except Exception as exc:
                self._record("proactive", "主动消息生成失败", {
                    "kind": kind, "error": str(exc)[:180],
                })
                return state, False
            if not message or message.upper() == "SKIP" or len(message) > 500:
                return state, False
            similarity = max((message_similarity(message, old) for old in recent), default=0)
            if similarity < self.settings["proactive_duplicate_threshold"]:
                break
            rejected = message
            message = ""
            self._record("proactive", "主动消息候选因近似重复被拒绝", {
                "kind": kind, "attempt": attempt, "similarity": similarity,
            })
        if not message:
            return state, False
        try:
            sent = await self.plugin.context.send_message(state["lastUmo"], MessageChain().message(message))
        except Exception as exc:
            self._record("proactive", "主动消息发送失败", {
                "kind": kind, "error": str(exc)[:180],
            })
            return state, False
        if not sent:
            return state, False
        if kind == "daytime_emergence":
            updated = await self.store.update(
                key,
                lambda current: record_daytime_emergence(
                    current,
                    message,
                    time_zone=self.settings["time_zone"],
                    memory_keys=selected_daytime_keys,
                ),
            )
        else:
            updated = await self.store.update(
                key, lambda current: record_bark(current, message, kind),
            )
        self._record("proactive", "主动消息已发送", {
            "scope": key,
            "kind": kind,
            "chars": len(message),
            "memory_sources": selected_daytime_keys if kind == "daytime_emergence" else [],
        })
        return updated, True

    async def _send_proactive(self, key: str, state: dict[str, Any]) -> dict[str, Any]:
        updated, _ = await self._send_active_message(key, state, "autonomous_thought")
        return updated

    async def status(self, key: str = "") -> dict[str, Any]:
        selected = self.scope_key_from_query(key)
        state = await self.store.read(selected)
        return self._state_payload(selected, state)

    async def all_states(self) -> dict[str, Any]:
        states = await self.store.snapshot()
        return {
            "settings": self.settings_payload(),
            "states": [self._state_payload(key, state, compact=True) for key, state in states.items()],
            "selected": next(iter(states), self.scope_key_from_query()),
        }

    def _state_payload(self, key: str, state: dict[str, Any], compact: bool = False) -> dict[str, Any]:
        payload = {
            "key": key,
            "revision": state["revision"],
            "consciousness": state["consciousness"],
            "fatigue": state["fatigue"],
            "lastConversationAt": state["lastConversationAt"],
            "lastSettledAt": state["lastSettledAt"],
            "intent": pick_intent(state),
            "topDrives": top_drives(state, 5),
        }
        if compact:
            return payload
        payload.update({
            "bodyState": self._body_state(scope_key=key),
            "drives": [
                {
                    "key": key_,
                    "label": DIMENSIONS[key_]["label"],
                    "kind": DIMENSIONS[key_].get("kind", "need"),
                    "value": effective_drive_value(state, key_),
                    "pressure": state["drives"][key_],
                    "activation": state["driveActivations"][key_],
                    "meta": dict(state.get("driveMeta", {}).get(key_) or {}),
                }
                for key_ in DRIVE_KEYS
            ],
            "driveHistory": list(state.get("driveHistory") or [])[-40:],
            "thoughtPool": state["thoughtPool"],
            "recentDreams": state["recentDreams"],
            "dreamBreathContext": breath_dream_context(state),
            "recentEvents": state["recentEvents"],
            "recentProactiveMessages": state["recentProactiveMessages"],
            "recentActiveMessages": recent_bark_history(state),
            "nextDaytimeEmergenceAt": state.get("nextDaytimeEmergenceAt"),
            "pendingAwareness": state.get("pendingAwareness"),
            "pendingOfflineAfterglow": state.get("pendingOfflineAfterglow"),
            "lastOfflineSessionId": state.get("lastOfflineSessionId"),
            "offlineEffectLedger": list(state.get("offlineEffectLedger") or [])[-20:],
            "lastInjection": self._last_injection.get(key),
            "lastPerception": self._last_perception.get(key),
            "perceptionHealth": self._perception_health_payload(),
            "perceptionHealthByStage": self._perception_health_by_stage_payload(),
            "lastBatch": copy.deepcopy(self._perception_batch_stats.get(key) or {}),
            "perceptionQueue": self._perception_queue_payload(key),
            "logs": self._event_history[-100:],
        })
        return payload

    def _perception_queue_payload(self, key: str) -> dict[str, Any]:
        worker = self._perception_workers.get(key)
        return {
            "pending": len(self._pending_turns.get(key) or []),
            "running": bool(worker is not None and not worker.done()),
            "coalesceEnable": bool(self.settings.get("post_perception_coalesce_enable", True)),
            "maxBatchTurns": int(self.settings.get("post_perception_max_batch_turns") or 4),
            "lastBatch": copy.deepcopy(self._perception_batch_stats.get(key) or {}),
        }

    async def settle_now(self, key: str) -> dict[str, Any]:
        selected = self.scope_key_from_query(key)
        state = await self._settle_one(selected, allow_generators=False)
        self._record("settle", "手动结算完成", {"scope": selected, "revision": state["revision"]})
        return self._state_payload(selected, state)

    async def feedback(self, key: str, drive: str, delta: float) -> dict[str, Any]:
        if drive not in DRIVE_KEYS:
            raise ValueError("未知驱动力")
        delta = _clamp(delta, -0.5, 0.5, 0)
        selected = self.scope_key_from_query(key)
        state = await self.store.update(
            selected, lambda current: apply_drive_feedback(current, {drive: delta}),
        )
        self._record("feedback", f"手动反馈 {drive} {delta:+.2f}", {"scope": selected})
        return self._state_payload(selected, state)

    async def add_thought(self, key: str, drive: str, text: str, intensity: float) -> dict[str, Any]:
        if drive not in DRIVE_KEYS:
            raise ValueError("未知驱动力")
        clean = " ".join(str(text or "").split())[:180]
        if not clean:
            raise ValueError("念头内容不能为空")
        selected = self.scope_key_from_query(key)
        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            add_flash_thought(current["thoughtPool"], drive, clean, _clamp(intensity, 0.1, 1, 0.6))
            current["revision"] += 1
            return current
        state = await self.store.update(selected, mutate)
        self._record("thought", "手动加入闪念", {"scope": selected, "drive": drive})
        return self._state_payload(selected, state)

    async def reset(self, key: str) -> dict[str, Any]:
        selected = self.scope_key_from_query(key)
        state = await self.store.reset(selected)
        self._record("reset", "心潮状态已重置", {"scope": selected})
        return self._state_payload(selected, state)

    async def simulate(self, key: str, hours: float, event: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = self.scope_key_from_query(key)
        state = await self.store.read(selected)
        future = datetime.fromtimestamp(time.time() + _clamp(hours, 0, 720, 0) * 3600, tz=timezone.utc)
        preview, meta = settle_state(
            state, future, self.settings["sleep_after_minutes"], self.settings["time_zone"],
            self.settings["dawn_freeze_start"], self.settings["dawn_freeze_end"],
        )
        if event:
            preview, _ = apply_conversation_event(preview, self._sanitize_event(event), future)
        payload = self._state_payload(selected, preview)
        block, diagnostics = self._render_injection(
            preview,
            body_state=self._body_state(future, selected),
        )
        payload["simulation"] = {"hours": hours, "meta": meta, "injection": block, "diagnostics": diagnostics}
        return payload
