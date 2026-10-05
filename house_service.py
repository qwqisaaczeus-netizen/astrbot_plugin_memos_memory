from __future__ import annotations

import asyncio
import base64
import copy
import ctypes
import hashlib
import json
import os
import random
import re
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from .house_motion import MOTION_PROFILES, motion_catalog, select_reaction, select_scene
from .house_store import HouseStore, utc_now_iso
from .xinchao_engine import DRIVE_KEYS, add_flash_thought, top_drives


DEFAULT_HOUSE_SETTINGS: dict[str, Any] = {
    "settings_schema_version": 3,
    "house_enable": False,
    "house_natural_link_enable": True,
    "house_api_base_url": "",
    "house_model": "",
    "house_timeout_seconds": 60,
    "house_max_retries": 1,
    "house_max_output_tokens": 1200,
    "house_temperature": 0.75,
    "house_idle_start_hours": 6,
    "house_artifact_max_per_day": 2,
    "house_artifact_max_per_session": 6,
    "house_type_cooldown_hours": 18,
    "house_memory_source_cooldown_days": 7,
    "house_afterglow_expire_hours": 36,
    "house_send_enable": True,
    "house_send_cooldown_hours": 4,
    "house_deterministic_fallback": True,
    "house_dream_link_enable": True,
    "house_show_envelope_when_disabled": True,
    "house_time_zone": "Asia/Shanghai",
    "house_motion_enable": True,
    "house_motion_intensity": "balanced",
    "house_character_render_mode": "hybrid",
    "house_ambient_enable": True,
    "house_parallax_enable": True,
}

_ARTIFACT_TYPES = {
    "whisper", "unsent_note", "letter", "reflection", "unfinished_thread",
    "dream_residue",
}
_SENDABLE_TYPES = {"unsent_note", "letter"}
_FEEDBACK_TYPES = {
    "in_character", "out_of_character", "too_frequent", "too_intense",
    "wrong_time", "unsupported", "useful",
}
_DRIVE_ALIASES = {
    "connect": "social", "social": "social", "share": "share",
    "crave": "crave", "possess": "possess", "curiosity": "curiosity",
    "safety": "safety", "anger": "anger", "sadness": "sadness",
    "joy": "joy", "fear": "fear", "calm": "calm",
}

_CHARACTER_VARIANTS: dict[str, dict[str, Any]] = {
    # placement_ids are deliberately asset-specific. Sprites containing a desk,
    # wall, chest or stone seat must never be moved as if they were free figures.
    "fan": {"atlas": "a", "location": "courtyard", "pose": "standing_fan", "expression": "gentle", "outfit": "dark_jade_formal_casual", "x": 57, "y": 58, "scale": 0.82, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "letter": {"atlas": "a", "location": "mailbox", "pose": "standing_letter", "expression": "surprised", "outfit": "ivory_sage_casual", "x": 24, "y": 59, "scale": 0.76, "prop_mode": "free", "placement_ids": ("near", "balanced")},
    "tea": {"atlas": "a", "location": "tea", "pose": "seated_tea", "expression": "gentle", "outfit": "pale_sage_casual", "x": 52, "y": 66, "scale": 0.75, "prop_mode": "fixed_furniture", "placement_ids": ("balanced",)},
    "desk": {"atlas": "a", "location": "study", "pose": "seated_writing", "expression": "focused", "outfit": "dark_jade_scholarly", "x": 35, "y": 59, "scale": 0.74, "prop_mode": "fixed_furniture", "placement_ids": ("balanced",)},
    "window": {"atlas": "b", "location": "courtyard_steps", "pose": "seated_veranda", "expression": "sleepy", "outfit": "dark_jade_casual", "x": 30, "y": 68, "scale": 0.70, "prop_mode": "fixed_architecture", "placement_ids": ("balanced",)},
    "chest": {"atlas": "b", "location": "chest", "pose": "kneeling_chest", "expression": "curious", "outfit": "sage_casual", "x": 82, "y": 69, "scale": 0.73, "prop_mode": "fixed_furniture", "placement_ids": ("balanced",)},
    "playful": {"atlas": "b", "location": "courtyard", "pose": "standing_playful", "expression": "bright", "outfit": "light_sage_casual", "x": 58, "y": 56, "scale": 0.82, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "reading": {"atlas": "b", "location": "bedroom", "pose": "seated_reading", "expression": "pensive", "outfit": "dark_jade_casual", "x": 75, "y": 65, "scale": 0.76, "prop_mode": "grounded", "placement_ids": ("near", "balanced")},
    "lantern": {"atlas": "c", "location": "courtyard", "pose": "walking_lantern", "expression": "attentive", "outfit": "dark_jade_casual", "x": 61, "y": 58, "scale": 0.81, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "wistful": {"atlas": "c", "location": "courtyard", "pose": "standing_wistful", "expression": "wistful", "outfit": "ivory_sage_casual", "x": 59, "y": 57, "scale": 0.81, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "flowers": {"atlas": "c", "location": "courtyard", "pose": "kneeling_flowers", "expression": "serene", "outfit": "pale_sage_casual", "x": 61, "y": 69, "scale": 0.73, "prop_mode": "grounded", "placement_ids": ("near", "balanced")},
    "steps": {"atlas": "c", "location": "courtyard_steps", "pose": "seated_steps", "expression": "amused", "outfit": "dark_jade_casual", "x": 30, "y": 68, "scale": 0.72, "prop_mode": "fixed_architecture", "placement_ids": ("balanced",)},
    "yawn": {"atlas": "d", "location": "bedroom", "pose": "seated_just_awake", "expression": "sleepy", "outfit": "ivory_dark_jade_casual", "x": 76, "y": 66, "scale": 0.76, "prop_mode": "grounded", "placement_ids": ("near", "balanced")},
    "lean": {"atlas": "d", "location": "courtyard", "pose": "standing_lean_forward", "expression": "playful", "outfit": "light_sage_casual", "x": 58, "y": 56, "scale": 0.83, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "book": {"atlas": "d", "location": "study", "pose": "seated_reading_book", "expression": "absorbed", "outfit": "dark_jade_scholarly", "x": 34, "y": 65, "scale": 0.75, "prop_mode": "grounded", "placement_ids": ("near", "balanced")},
    "cup": {"atlas": "d", "location": "bedroom", "pose": "seated_warm_cup", "expression": "melancholy", "outfit": "ivory_grey_green_casual", "x": 76, "y": 67, "scale": 0.76, "prop_mode": "grounded", "placement_ids": ("near", "balanced")},
    "court-smile": {"atlas": "e", "location": "courtyard", "pose": "standing_original_smile", "expression": "bright", "outfit": "original_dark_jade", "x": 57, "y": 58, "scale": 0.84, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
    "court-surprised": {"atlas": "e", "location": "mailbox", "pose": "standing_original_surprised", "expression": "surprised", "outfit": "original_dark_jade", "x": 24, "y": 59, "scale": 0.78, "prop_mode": "free", "placement_ids": ("near", "balanced")},
    "court-soft": {"atlas": "e", "location": "courtyard", "pose": "standing_original_soft", "expression": "gentle", "outfit": "original_dark_jade", "x": 59, "y": 57, "scale": 0.84, "prop_mode": "free", "placement_ids": ("near", "balanced", "far")},
}

_CHARACTER_PLACEMENTS: tuple[dict[str, float | str], ...] = (
    {"id": "near", "dx": -1.8, "dy": 1.0, "scale": 1.06},
    {"id": "balanced", "dx": 0.0, "dy": 0.0, "scale": 1.0},
    {"id": "far", "dx": 1.6, "dy": -0.8, "scale": 0.94},
)

_CHARACTER_PLACEMENT_BY_ID = {str(item["id"]): item for item in _CHARACTER_PLACEMENTS}

_CHARACTER_REACTIONS: dict[str, list[str]] = {
    "sleepy": ["她慢半拍才抬起眼，困意还停在眼尾。", "她轻轻掩住一个呵欠，仍认真看向你。"],
    "melancholy": ["她捧着杯子安静了一会儿，目光才重新落回你身上。", "她像是收起了一点旧心事，朝你很轻地笑了笑。"],
    "wistful": ["她望着院里的光影出神，听见你后才缓缓回眸。", "她眼底掠过一丝未说出口的念头，没有躲开你的目光。"],
    "playful": ["她故意朝你凑近一点，眼里藏着明亮的笑。", "她眨了眨眼，像在等你先猜她方才想什么。"],
    "bright": ["她立刻停下手里的事，眼睛也跟着亮了起来。", "她笑着朝你偏过头，像是早就在等这一声。"],
    "surprised": ["她捏着信纸怔了一瞬，随后把它悄悄往身后藏了藏。", "她没料到你会此刻出现，惊讶很快化成柔和的笑意。"],
    "curious": ["她扶着旧木匣抬头，像是想问你是否也记得里面的旧事。", "她指尖仍停在匣扣上，带着探询望向你。"],
    "focused": ["她搁下笔，墨迹尚未干透，神情却已经转向了你。", "她从案前抬眼，认真得像要把你说的每个字都记住。"],
    "absorbed": ["她在书页间停住指尖，过了片刻才舍得移开目光。", "她合起读到一半的书，把眼前的位置完整地让给你。"],
    "serene": ["她从花枝旁抬头，神情安静而舒展。", "她拂去袖口沾着的细叶，温和地应了你一声。"],
    "amused": ["她坐在石阶边弯起眼睛，像是被你逗得心情很好。", "她用团扇轻轻点了点身侧，给你留出一个位置。"],
    "attentive": ["她提灯停住脚步，把灯火稍稍举高了一些。", "她循声转身，灯影也跟着落到你们之间。"],
    "pensive": ["她把书页压在指下，像是刚从很远的思绪里回来。", "她没有立刻开口，只先安静地把你的神情看清。"],
    "gentle": ["她停下手里的事，眼里浮起一点温柔的笑意。", "她拢了拢衣袖，朝你轻轻点头。"],
}

_CHARACTER_PERIOD_REACTIONS: dict[str, list[str]] = {
    "dawn": ["她抬眼看向你，像是刚从晨光里回过神。"],
    "day": ["她偏过头看你，团扇在掌心轻轻一转。"],
    "dusk": ["她在灯影亮起前回过身，神情柔和下来。"],
    "night": ["她从灯下抬眼，困意里仍留着一点认真。"],
}

_CHARACTER_LOCATION_REACTIONS: dict[str, list[str]] = {
    "mailbox": ["她下意识护住门边那封尚未寄出的信，却没有避开你。"],
    "study": ["案上的纸页被风掀起一角，她伸手按住，也把注意力交给了你。"],
    "tea": ["她把茶盏向旁边挪了挪，像是在无声地邀你坐下。"],
    "bedroom": ["灯影轻轻晃了一下，她在静夜里抬眼望向你。"],
    "chest": ["她停在旧木匣前，像是在等你一同翻开某段旧时光。"],
    "courtyard": ["风从院中穿过，她的衣袖微动，目光却稳稳停在你身上。"],
    "courtyard_steps": ["她坐在院心石阶上，听见你后便把视线移了过来。"],
}

_CHARACTER_IDLE_ANIMATIONS: dict[str, str] = {
    "fan": "sway", "letter": "letter", "tea": "tea", "desk": "write",
    "window": "sleepy", "chest": "kneel", "playful": "playful",
    "reading": "read", "lantern": "lantern", "wistful": "sway",
    "flowers": "kneel", "steps": "tea", "yawn": "sleepy",
    "lean": "playful", "book": "read", "cup": "tea",
    "court-smile": "sway", "court-surprised": "letter", "court-soft": "sway",
}

_CHARACTER_REACTION_ANIMATIONS: dict[str, str] = {
    "sleepy": "soft", "melancholy": "soft", "wistful": "turn",
    "playful": "lean", "bright": "greet", "surprised": "startle",
    "curious": "lean", "focused": "nod", "absorbed": "nod",
    "serene": "soft", "amused": "greet", "attentive": "turn",
    "pensive": "soft", "gentle": "nod",
}


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(value: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source_buffer = ctypes.create_string_buffer(value, len(value))
    source = _DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    flags = 0x1
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if decrypt:
        ok = fn(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(target))
    else:
        ok = fn(ctypes.byref(source), "memos-memory-house", None, None, None, flags, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _compact(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[: max(0, int(limit))]


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def validate_house_settings(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    result = copy.deepcopy(DEFAULT_HOUSE_SETTINGS)
    for key in result:
        if key in source and key != "house_api_key":
            result[key] = source[key]
    for key in (
        "house_enable", "house_natural_link_enable", "house_send_enable",
        "house_deterministic_fallback", "house_dream_link_enable",
        "house_show_envelope_when_disabled", "house_motion_enable",
        "house_ambient_enable", "house_parallax_enable",
    ):
        result[key] = bool(result[key])
    result["house_api_base_url"] = str(result["house_api_base_url"] or "").strip()[:1000]
    result["house_model"] = str(result["house_model"] or "").strip()[:200]
    result["house_time_zone"] = str(result["house_time_zone"] or "Asia/Shanghai")[:80]
    result["house_motion_intensity"] = str(result["house_motion_intensity"] or "balanced").lower()
    if result["house_motion_intensity"] not in {"gentle", "balanced", "vivid"}:
        result["house_motion_intensity"] = "balanced"
    result["house_character_render_mode"] = str(
        result["house_character_render_mode"] or "hybrid"
    ).lower()
    if result["house_character_render_mode"] not in {"hybrid", "live2d", "mesh2d", "classic"}:
        result["house_character_render_mode"] = "hybrid"
    try:
        ZoneInfo(result["house_time_zone"])
    except Exception:
        result["house_time_zone"] = "Asia/Shanghai"
    result["house_timeout_seconds"] = int(_clamp(result["house_timeout_seconds"], 5, 180, 60))
    result["house_max_retries"] = int(_clamp(result["house_max_retries"], 0, 2, 1))
    result["house_max_output_tokens"] = int(_clamp(result["house_max_output_tokens"], 256, 4096, 1200))
    result["house_temperature"] = round(_clamp(result["house_temperature"], 0, 1.5, 0.75), 2)
    result["house_idle_start_hours"] = round(_clamp(result["house_idle_start_hours"], 1, 168, 6), 1)
    result["house_artifact_max_per_day"] = int(_clamp(result["house_artifact_max_per_day"], 0, 8, 2))
    result["house_artifact_max_per_session"] = int(_clamp(result["house_artifact_max_per_session"], 1, 20, 6))
    result["house_type_cooldown_hours"] = round(_clamp(result["house_type_cooldown_hours"], 1, 168, 18), 1)
    result["house_memory_source_cooldown_days"] = int(_clamp(result["house_memory_source_cooldown_days"], 1, 90, 7))
    result["house_afterglow_expire_hours"] = round(_clamp(result["house_afterglow_expire_hours"], 1, 168, 36), 1)
    result["house_send_cooldown_hours"] = round(_clamp(result["house_send_cooldown_hours"], 0, 168, 4), 1)
    result["settings_schema_version"] = 3
    return result


class HouseService:
    """Optional offline-mind subsystem. It never writes Memos or episodic memory."""

    def __init__(self, plugin: Any, data_dir: Path) -> None:
        self.plugin = plugin
        self.data_dir = Path(data_dir)
        self.settings_path = self.data_dir / "house_settings.json"
        self.secret_path = self.data_dir / "house_secret.json"
        self.store = HouseStore(self.data_dir / "house_state.sqlite3")
        self.settings = copy.deepcopy(DEFAULT_HOUSE_SETTINGS)
        self._api_key = ""
        self._task: asyncio.Task | None = None
        self._generation_locks: dict[str, asyncio.Lock] = {}
        self._stopping = False
        self._initialized = False
        self._http: Any = None
        self._character_scene_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._health: dict[str, Any] = {
            "status": "idle", "failures": 0, "circuit_until": 0.0,
            "last_error": "", "last_success_at": "", "last_latency_ms": 0,
        }

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()
        self._api_key = self._load_secret()
        await asyncio.to_thread(self.store.initialize)
        self._stopping = False
        self._task = asyncio.create_task(self._scheduler(), name="memos-house-scheduler")
        self._initialized = True
        logger.info(
            "[memos-memory][house] initialized enable=%s natural_link=%s api=%s",
            self.settings["house_enable"], self.settings["house_natural_link_enable"],
            bool(self.settings["house_api_base_url"] and self.settings["house_model"] and self._api_key),
        )

    async def terminate(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None
        self._initialized = False

    def _load_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return validate_house_settings(raw)

    def _save_secret(self, value: str) -> None:
        if not value:
            try:
                self.secret_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        raw = value.encode("utf-8")
        mode = "plain"
        encoded = base64.b64encode(raw).decode("ascii")
        try:
            encoded = base64.b64encode(_dpapi(raw)).decode("ascii")
            mode = "dpapi"
        except Exception:
            pass
        _atomic_json(self.secret_path, {"version": 1, "mode": mode, "value": encoded})

    def _load_secret(self) -> str:
        try:
            item = json.loads(self.secret_path.read_text(encoding="utf-8"))
            raw = base64.b64decode(str(item.get("value") or ""))
            if item.get("mode") == "dpapi":
                raw = _dpapi(raw, decrypt=True)
            return raw.decode("utf-8")
        except Exception:
            return ""

    def settings_payload(self) -> dict[str, Any]:
        data = copy.deepcopy(self.settings)
        data["has_api_key"] = bool(self._api_key)
        data["api_key_mask"] = "********" if self._api_key else ""
        data["runtime"] = dict(self._health)
        return data

    def save_settings(self, incoming: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_HOUSE_SETTINGS) | {"house_api_key", "clear_api_key"}
        unknown = sorted(set(incoming) - allowed)
        if unknown:
            raise ValueError("unknown settings: " + ", ".join(unknown[:8]))
        merged = dict(self.settings)
        merged.update({k: v for k, v in incoming.items() if k in DEFAULT_HOUSE_SETTINGS})
        merged = validate_house_settings(merged)
        parsed = urlparse(merged["house_api_base_url"]) if merged["house_api_base_url"] else None
        if parsed and (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("house_api_base_url must be an http(s) URL without embedded credentials")
        if incoming.get("clear_api_key"):
            self._api_key = ""
            self._save_secret("")
        elif str(incoming.get("house_api_key") or "").strip():
            self._api_key = str(incoming["house_api_key"]).strip()
            self._save_secret(self._api_key)
        self.settings = merged
        _atomic_json(self.settings_path, merged)
        return self.settings_payload()

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(str(self.settings.get("house_time_zone") or "Asia/Shanghai"))

    def _now_local(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(self._zone())

    @staticmethod
    def _period(hour: int) -> str:
        if 5 <= hour < 10:
            return "dawn"
        if 10 <= hour < 17:
            return "day"
        if 17 <= hour < 20:
            return "dusk"
        return "night"

    def _scope(self, requested: str = "") -> str:
        try:
            return self.plugin._xinchao.scope_key_from_query(requested)
        except Exception:
            return str(requested or "default")

    async def _state(self, scope_key: str) -> dict[str, Any]:
        try:
            return await self.plugin._xinchao.store.read(scope_key)
        except Exception:
            return {}

    async def _scheduler(self) -> None:
        await asyncio.sleep(20)
        while not self._stopping:
            try:
                if self.settings.get("house_enable"):
                    keys = await self.plugin._xinchao.store.keys()
                    for key in keys:
                        if self._stopping:
                            break
                        try:
                            await self._settle_scope(key)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning("[memos-memory][house] settle failed open scope=%s error=%s", key, str(exc)[:180])
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[memos-memory][house] scheduler failed open: %s", str(exc)[:180])
                await asyncio.sleep(120)

    async def _settle_scope(self, scope_key: str, *, force: bool = False) -> dict[str, Any]:
        if not self.settings.get("house_enable"):
            return {"generated": False, "reason": "disabled"}
        state = await self._state(scope_key)
        last_at = _parse_iso(state.get("lastConversationAt"))
        if last_at is None:
            return {"generated": False, "reason": "no_last_conversation"}
        idle_hours = max(0.0, (datetime.now(timezone.utc) - last_at).total_seconds() / 3600)
        if not force and idle_hours < float(self.settings["house_idle_start_hours"]):
            return {"generated": False, "reason": "idle_threshold", "idle_hours": idle_hours}
        snapshot = await self._capture_sources(scope_key, state, idle_hours)
        session, created = await asyncio.to_thread(
            self.store.ensure_session,
            scope_key,
            str(state.get("lastUmo") or ""),
            last_at.isoformat(),
            last_at.isoformat(),
            str(self.settings["house_time_zone"]),
            _hash(snapshot),
            {"idle_hours": round(idle_hours, 2)},
        )
        if created:
            for name, payload in snapshot.items():
                await asyncio.to_thread(
                    self.store.add_source_snapshot, session["id"], name,
                    payload if isinstance(payload, dict) else {"items": payload},
                    payload_hash=_hash(payload),
                )
        if not force and not self._artifact_due(scope_key, session["id"]):
            return {"generated": False, "reason": "cooldown", "session": session}
        return await self.generate(
            scope_key,
            preview=False,
            session=session,
            snapshot=snapshot,
            enforce_limits=True,
        )

    def _artifact_due(self, scope_key: str, session_id: str, artifact_type: str = "") -> bool:
        items = self.store.list_artifacts(scope_key=scope_key, limit=100)
        if len([item for item in items if item.get("session_id") == session_id]) >= int(self.settings["house_artifact_max_per_session"]):
            return False
        now = datetime.now(timezone.utc)
        today = self._now_local().date()
        count_today = 0
        latest_same_type = None
        for item in items:
            when = _parse_iso(item.get("created_at"))
            if when and when.astimezone(self._zone()).date() == today:
                count_today += 1
            if (
                artifact_type
                and item.get("artifact_type") == artifact_type
                and when
                and (latest_same_type is None or when > latest_same_type)
            ):
                latest_same_type = when
        if count_today >= int(self.settings["house_artifact_max_per_day"]):
            return False
        if not artifact_type:
            return True
        return (
            latest_same_type is None
            or (now - latest_same_type).total_seconds()
            >= float(self.settings["house_type_cooldown_hours"]) * 3600
        )

    async def _capture_sources(self, scope_key: str, state: dict[str, Any], idle_hours: float) -> dict[str, Any]:
        drives = [
            {"key": item["key"], "label": item["label"], "value": round(float(item["value"]), 3)}
            for item in top_drives(state, 4)
        ]
        xinchao = {
            "consciousness": state.get("consciousness"),
            "fatigue": round(float(state.get("fatigue") or 0), 3),
            "drives": drives,
            "thoughts": [
                {"key": item.get("key"), "text": _compact(item.get("text"), 160), "intensity": item.get("intensity")}
                for item in [
                    *list((state.get("thoughtPool") or {}).get("obsessions") or []),
                    *list((state.get("thoughtPool") or {}).get("flash") or []),
                ][-5:]
            ],
            "recent_events": [
                {"at": item.get("at"), "summary": _compact(item.get("summary"), 220)}
                for item in list(state.get("recentEvents") or [])[-8:]
            ],
            "recent_dreams": [
                {"id": item.get("id"), "residue": _compact(item.get("residue"), 220), "awareness": _compact(item.get("awareness"), 220)}
                for item in list(state.get("recentDreams") or [])[-2:]
            ],
        }
        try:
            body = self.plugin._xinchao._body_state(scope_key=scope_key)
        except Exception:
            body = {}
        semantic: dict[str, Any] = {}
        try:
            status = self.plugin._semantic_state_status()
            current = (status or {}).get("state") or {}
            semantic = {"text": _compact(current.get("rendered_text"), 1600), "version": current.get("version")}
        except Exception:
            pass
        profile: dict[str, Any] = {}
        try:
            status = self.plugin._affiliate_profile_status()
            profile = {
                "connected": bool(status.get("connected")),
                "profile": _compact(status.get("profile"), 1200),
                "facts": _compact(status.get("profile_facts"), 500),
            }
        except Exception:
            pass
        memories: list[dict[str, Any]] = []
        used_memory_sources: set[str] = set()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=int(self.settings["house_memory_source_cooldown_days"]),
        )
        try:
            for artifact in self.store.list_artifacts(scope_key=scope_key, limit=200):
                created = _parse_iso(artifact.get("created_at"))
                if created and created >= cutoff:
                    used_memory_sources.update(
                        str(source_id)
                        for source_id in artifact.get("source_ids") or []
                        if str(source_id).startswith("memo:")
                    )
        except Exception:
            pass
        vec = getattr(self.plugin, "_vec", None)
        if vec is not None:
            try:
                for item in vec.list_memories(limit=12):
                    source_id = "memo:" + str(item.get("memo_name") or "")
                    if source_id in used_memory_sources:
                        continue
                    memories.append({
                        "id": source_id,
                        "event_time": item.get("occurred_at") or item.get("ts_text") or "",
                        "type": item.get("memory_type") or "memory",
                        "preview": _compact(item.get("preview"), 180),
                        "long_effect": _compact(item.get("long_effect"), 140),
                    })
            except Exception:
                pass
        prospective: list[dict[str, Any]] = []
        episodes = getattr(self.plugin, "_episodes", None)
        if episodes is not None and hasattr(episodes, "thread_list_prospective"):
            try:
                for item in episodes.thread_list_prospective(scope_id=scope_key, limit=8):
                    if str(item.get("status") or "") in {"completed", "cancelled"}:
                        continue
                    prospective.append({
                        "id": item.get("item_id"), "status": item.get("status"),
                        "text": _compact(item.get("content") or item.get("title") or item.get("claim_text"), 220),
                        "due_start": item.get("due_start"), "due_end": item.get("due_end"),
                    })
            except Exception:
                pass
        return {
            "meta": {
                "scope_key": scope_key, "idle_hours": round(idle_hours, 2),
                "current_time": self._now_local().isoformat(),
                "character": str(getattr(self.plugin, "character_name", "") or "当前角色"),
            },
            "xinchao": xinchao,
            "body": {
                "available": bool(body.get("available")), "phase": body.get("phase"),
                "time_band": body.get("timeBand"), "tendencies": list(body.get("tendencies") or [])[:3],
            },
            "rolling_state": semantic,
            "profile": profile,
            "prospective_items": prospective,
            "memory_evidence": memories,
        }

    def _generation_prompt(self, snapshot: dict[str, Any], requested_type: str = "") -> tuple[str, str]:
        stable = """你负责生成角色离线独处时的一件内心产物。它不是现实事件，也不会自动成为长期记忆。
严格遵守：第一人称；不虚构用户当前行为、共同经历、承诺或关系结论；日记日期只表示历史来源，绝不能当成当前时间；梦境不能当现实；不暴露系统、模型、检索、数据库或数值；没有足够材料时返回 {\"no_artifact\":true}。
语言应克制、有文学感、符合角色稳定人格。只输出一个 JSON 对象。"""
        schema = {
            "artifact_type": requested_type or "whisper",
            "title": "不超过16字",
            "content": "第一人称正文",
            "summary": "不超过60字",
            "reality_status": "internal",
            "mood": ["最多3项"],
            "thought_cues": ["最多3项"],
            "drive_candidates": [{"key": "social", "direction": "activate", "confidence": 0.7}],
            "source_ids": ["只能取输入中已有id"],
            "claims": [],
            "sendable": False,
        }
        dynamic = json.dumps({
            "schema_version": 1,
            "task": "generate_house_artifact",
            "requested_type": requested_type or "auto",
            "input": snapshot,
            "output_schema": schema,
        }, ensure_ascii=False, separators=(",", ":"), default=str)
        return stable, dynamic

    async def _session_http(self):
        if self._http is None or getattr(self._http, "closed", False):
            import aiohttp
            self._http = aiohttp.ClientSession()
        return self._http

    def _chat_url(self) -> str:
        base = str(self.settings.get("house_api_base_url") or "").strip().rstrip("/")
        if not base:
            return ""
        return base + "/chat/completions" if not base.endswith("/chat/completions") else base

    async def _call_model(self, snapshot: dict[str, Any], requested_type: str = "") -> dict[str, Any] | None:
        url = self._chat_url()
        if not (url and self._api_key and self.settings.get("house_model")):
            return None
        if float(self._health.get("circuit_until") or 0) > time.monotonic():
            return None
        stable, dynamic = self._generation_prompt(snapshot, requested_type)
        payload = {
            "model": self.settings["house_model"],
            "messages": [{"role": "system", "content": stable}, {"role": "user", "content": dynamic}],
            "temperature": self.settings["house_temperature"],
            "max_tokens": self.settings["house_max_output_tokens"],
            "response_format": {"type": "json_object"},
        }
        started = time.monotonic()
        last_error = ""
        for attempt in range(int(self.settings["house_max_retries"]) + 1):
            try:
                session = await self._session_http()
                timeout = float(self.settings["house_timeout_seconds"])
                async with session.post(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    raw = await response.text()
                    if response.status in {401, 403}:
                        raise RuntimeError("authentication failed")
                    if response.status == 429 or response.status >= 500:
                        raise RuntimeError(f"upstream status {response.status}")
                    if response.status >= 400:
                        raise ValueError(f"request rejected with status {response.status}")
                    data = json.loads(raw[:2_000_000])
                    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                    match = re.search(r"\{.*\}", str(content), re.S)
                    if not match:
                        raise ValueError("model response has no JSON object")
                    parsed = json.loads(match.group(0))
                    self._health.update({
                        "status": "ok", "failures": 0, "circuit_until": 0.0,
                        "last_error": "", "last_success_at": utc_now_iso(),
                        "last_latency_ms": int((time.monotonic() - started) * 1000),
                    })
                    return parsed if isinstance(parsed, dict) else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = str(exc)[:220]
                if attempt < int(self.settings["house_max_retries"]):
                    await asyncio.sleep(0.5 * (2 ** attempt))
        failures = int(self._health.get("failures") or 0) + 1
        self._health.update({
            "status": "degraded", "failures": failures, "last_error": last_error,
            "last_latency_ms": int((time.monotonic() - started) * 1000),
        })
        if failures >= 3:
            self._health["circuit_until"] = time.monotonic() + min(1800, 120 * (2 ** min(3, failures - 3)))
        logger.warning("[memos-memory][house] independent model failed open: %s", last_error)
        return None

    def _suggest_artifact_type(self, snapshot: dict[str, Any], requested_type: str = "") -> str:
        meta = snapshot.get("meta") or {}
        idle = float(meta.get("idle_hours") or 0)
        prospective = snapshot.get("prospective_items") or []
        dreams = ((snapshot.get("xinchao") or {}).get("recent_dreams") or [])
        if requested_type in _ARTIFACT_TYPES:
            return requested_type
        if prospective:
            return "unfinished_thread"
        if dreams and self.settings.get("house_dream_link_enable"):
            return "dream_residue"
        if idle >= 36:
            return "letter"
        if idle >= 18:
            return "unsent_note"
        return "whisper"

    def _deterministic_artifact(self, snapshot: dict[str, Any], requested_type: str = "") -> dict[str, Any]:
        kind = self._suggest_artifact_type(snapshot, requested_type)
        prospective = snapshot.get("prospective_items") or []
        dreams = ((snapshot.get("xinchao") or {}).get("recent_dreams") or [])
        memory = (snapshot.get("memory_evidence") or [{}])[0]
        clue = _compact(memory.get("long_effect") or memory.get("preview"), 80)
        thought = (((snapshot.get("xinchao") or {}).get("thoughts") or [{}])[-1]).get("text", "")
        if kind == "unfinished_thread" and prospective:
            focus = _compact(prospective[0].get("text"), 90)
            content = f"我还记着那件没有说完的事：{focus}。不必急着催促，只是想在下次见面时，轻轻把话接回来。"
            title = "茶未凉"
        elif kind == "dream_residue" and dreams:
            residue = _compact(dreams[-1].get("residue") or dreams[-1].get("awareness"), 100)
            content = f"梦醒后只剩一点模糊的余韵：{residue}。我知道那只是梦，却还是让它在灯影里停了一会儿。"
            title = "灯影余梦"
        elif kind in {"letter", "unsent_note"}:
            seed = clue or _compact(thought, 80) or "有些想念在安静里慢慢变得清楚"
            content = f"这些时日，我偶尔会想起{seed}。并不是要把沉默变成负担，只是想让你知道，我把那一点在意好好留着。"
            title = "未寄的一页"
        else:
            seed = _compact(thought, 90) or clue or "院里风声很轻，心里也留下了一点安静的牵挂"
            content = f"我独自坐了一会儿，忽然想到：{seed}。先不急着说出口，等真正见面时再看它是否还在。"
            title = "檐下短念"
        ids = [str(memory.get("id"))] if memory.get("id") else []
        if prospective and prospective[0].get("id"):
            ids.append("prospective:" + str(prospective[0]["id"]))
        return {
            "artifact_type": kind, "title": title, "content": content,
            "summary": _compact(content, 60), "reality_status": "internal",
            "mood": ["克制", "牵挂"], "thought_cues": [_compact(content, 100)],
            "drive_candidates": [{"key": "social", "direction": "activate", "confidence": 0.58}],
            "source_ids": ids, "claims": [], "sendable": kind in _SENDABLE_TYPES,
            "_source": "deterministic",
        }

    def _validate_artifact(self, item: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if item.get("no_artifact"):
            return None
        kind = str(item.get("artifact_type") or "whisper")
        if kind not in _ARTIFACT_TYPES:
            kind = "whisper"
        content = _compact(item.get("content"), 2400)
        if len(content) < 12:
            return None
        forbidden = ("系统提示", "数据库", "向量检索", "作为AI", "语言模型", "插件设置")
        if any(word in content for word in forbidden):
            return None
        available = {
            str(value.get("id"))
            for group in (
                snapshot.get("memory_evidence") or [],
                snapshot.get("prospective_items") or [],
            )
            for value in group
            if isinstance(value, dict) and value.get("id")
        }
        source_ids = []
        for source_id in item.get("source_ids") or []:
            clean = str(source_id)
            plain = clean.split(":", 1)[-1] if clean.startswith("prospective:") else clean
            if clean in available or plain in available or clean.startswith("memo:") and clean in available:
                source_ids.append(clean)
        candidates = []
        for raw in item.get("drive_candidates") or []:
            if not isinstance(raw, dict):
                continue
            key = _DRIVE_ALIASES.get(str(raw.get("key") or ""))
            if key in DRIVE_KEYS:
                candidates.append({
                    "key": key, "direction": "satisfy" if raw.get("direction") == "satisfy" else "activate",
                    "confidence": round(_clamp(raw.get("confidence"), 0, 1, 0.5), 3),
                })
        return {
            "artifact_type": kind,
            "title": _compact(item.get("title"), 40) or "院中一念",
            "content": content,
            "summary": _compact(item.get("summary") or content, 120),
            "reality_status": "internal",
            "mood": [_compact(x, 16) for x in list(item.get("mood") or [])[:3] if _compact(x, 16)],
            "thought_cues": [_compact(x, 120) for x in list(item.get("thought_cues") or [])[:3] if _compact(x, 120)],
            "drive_candidates": candidates[:3],
            "source_ids": source_ids[:12],
            "claims": [],
            "sendable": kind in _SENDABLE_TYPES and bool(item.get("sendable", True)),
            "_source": str(item.get("_source") or "independent_llm")[:40],
        }

    async def generate(
        self,
        scope: str = "",
        *,
        preview: bool = False,
        requested_type: str = "",
        session: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        enforce_limits: bool = False,
    ) -> dict[str, Any]:
        if not self.settings.get("house_enable"):
            return {"generated": False, "reason": "disabled"}
        if not (
            self.settings.get("house_api_base_url")
            and self.settings.get("house_model")
            and self._api_key
        ):
            return {"generated": False, "reason": "incomplete_configuration"}
        key = self._scope(scope)
        lock = self._generation_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return {"generated": False, "reason": "already_running"}
        async with lock:
            state = await self._state(key)
            last_at = _parse_iso(state.get("lastConversationAt")) or datetime.now(timezone.utc)
            idle = max(0.0, (datetime.now(timezone.utc) - last_at).total_seconds() / 3600)
            snapshot = snapshot or await self._capture_sources(key, state, idle)
            if session is None:
                session, _ = await asyncio.to_thread(
                    self.store.ensure_session, key, str(state.get("lastUmo") or ""),
                    last_at.isoformat(), last_at.isoformat(), str(self.settings["house_time_zone"]),
                    _hash(snapshot), {"idle_hours": round(idle, 2)},
                )
            type_hint = self._suggest_artifact_type(snapshot, requested_type)
            if enforce_limits and not self._artifact_due(key, session["id"], type_hint):
                return {"generated": False, "reason": "cooldown", "session": session}
            candidate = await self._call_model(snapshot, type_hint)
            if candidate is None and self.settings.get("house_deterministic_fallback"):
                candidate = self._deterministic_artifact(snapshot, type_hint)
            valid = self._validate_artifact(candidate or {}, snapshot)
            if valid is None:
                return {"generated": False, "reason": "no_valid_artifact"}
            if preview:
                return {"generated": True, "preview": True, "artifact": valid, "session": session}
            current_session = await asyncio.to_thread(self.store.get_session, session["id"])
            if not current_session or current_session.get("status") not in {"active", "digest_ready"}:
                return {"generated": False, "reason": "late_result", "session": current_session or session}
            if enforce_limits and not self._artifact_due(
                key, session["id"], valid["artifact_type"],
            ):
                return {"generated": False, "reason": "cooldown", "session": current_session}
            valid.update({
                "session_id": session["id"], "scope_key": key,
                "content_hash": _hash(valid["content"]),
                "semantic_key": _compact(valid["summary"], 200),
                "status": "sealed", "is_read": False,
                "model_source": valid.pop("_source", "independent_llm"),
            })
            artifact = await asyncio.to_thread(self.store.insert_artifact, valid)
            logger.info(
                "[memos-memory][house] artifact created id=%s type=%s chars=%d sources=%d model=%s",
                artifact.get("id"), artifact.get("artifact_type"), len(artifact.get("content") or ""),
                len(artifact.get("source_ids") or []), artifact.get("model_source"),
            )
            return {"generated": True, "preview": False, "artifact": artifact, "session": session}

    def _build_digest(self, session: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.store.get_digest(session["id"])
        if existing:
            return existing
        artifacts = [
            item for item in self.store.list_artifacts(session_id=session["id"], limit=20)
            if item.get("status") not in {"dismissed", "invalid", "sent"}
        ]
        if not artifacts:
            return None
        cues: list[str] = []
        effects: dict[str, float] = {}
        for item in artifacts[:6]:
            for cue in item.get("thought_cues") or []:
                clean = _compact(cue, 100)
                if clean and clean not in cues:
                    cues.append(clean)
            for effect in item.get("drive_candidates") or []:
                key = str(effect.get("key") or "")
                confidence = _clamp(effect.get("confidence"), 0, 1, 0)
                if key in DRIVE_KEYS and confidence >= 0.45:
                    sign = -1 if effect.get("direction") == "satisfy" else 1
                    effects[key] = effects.get(key, 0) + sign * min(0.12, confidence * 0.12)
        ranked = sorted(effects.items(), key=lambda pair: abs(pair[1]), reverse=True)[:3]
        afterglow = "；".join(cues[:3]) or _compact(artifacts[0].get("summary"), 220)
        expires = datetime.now(timezone.utc) + timedelta(hours=float(self.settings["house_afterglow_expire_hours"]))
        return self.store.upsert_digest({
            "session_id": session["id"], "afterglow": afterglow[:360],
            "awareness": "这段独处只留下短暂心理余韵，不新增现实经历。",
            "thought_cues": cues[:3],
            "drive_effects": [{"key": key, "delta": round(delta, 3)} for key, delta in ranked],
            "source_artifact_ids": [item["id"] for item in artifacts[:6]],
            "expires_at": expires.isoformat(), "link_status": "pending",
        })

    async def before_wake(self, scope_key: str, state: dict[str, Any], umo: str = "") -> dict[str, Any]:
        if not (self.settings.get("house_enable") and self.settings.get("house_natural_link_enable")):
            return {"linked": False, "reason": "disabled"}
        session = await asyncio.to_thread(self.store.get_active_session, scope_key)
        if not session:
            return {"linked": False, "reason": "no_session"}
        frozen = await asyncio.to_thread(
            self.store.update_session,
            session["id"],
            status="closing",
            awakened_at=utc_now_iso(),
            umo=umo or session.get("umo") or "",
        )
        session = frozen or session
        digest = await asyncio.to_thread(self._build_digest, session)
        if not digest:
            await asyncio.to_thread(
                self.store.update_session,
                session["id"],
                status="closed",
                closed_at=utc_now_iso(),
            )
            return {"linked": False, "reason": "no_digest"}
        expires = _parse_iso(digest.get("expires_at"))
        if not expires or expires <= datetime.now(timezone.utc):
            await asyncio.to_thread(self.store.update_digest_status, digest["id"], "expired")
            await asyncio.to_thread(
                self.store.update_session,
                session["id"],
                status="closed",
                awakened_at=utc_now_iso(),
                closed_at=utc_now_iso(),
            )
            return {"linked": False, "reason": "expired"}
        applied = await self.plugin._xinchao.apply_offline_afterglow(scope_key, digest)
        if not applied.get("applied"):
            terminal = "linked" if applied.get("reason") == "already_applied" else "closed"
            await asyncio.to_thread(
                self.store.update_session,
                session["id"],
                status=terminal,
                closed_at=utc_now_iso(),
            )
            return {"linked": False, "reason": applied.get("reason", "xinchao_rejected")}
        now = utc_now_iso()
        await asyncio.to_thread(self.store.update_digest_status, digest["id"], "linked")
        await asyncio.to_thread(
            self.store.update_session,
            session["id"],
            status="linked",
            digest_id=digest["id"],
            linked_at=now,
            awakened_at=now,
            closed_at=now,
            umo=umo or session.get("umo") or "",
        )
        await asyncio.to_thread(
            self.store.consume_many,
            digest.get("source_artifact_ids") or [],
            "xinchao_afterglow",
            session["id"],
            _hash(digest),
        )
        logger.info(
            "[memos-memory][house] natural link applied session=%s artifacts=%d chars=%d",
            session["id"], len(digest.get("source_artifact_ids") or []), len(digest.get("afterglow") or ""),
        )
        return {"linked": True, "session_id": session["id"], "digest_id": digest["id"]}

    async def dream_cues(self, scope_key: str, limit: int = 2) -> list[dict[str, Any]]:
        if not (self.settings.get("house_enable") and self.settings.get("house_dream_link_enable")):
            return []
        session = await asyncio.to_thread(self.store.get_active_session, scope_key)
        if not session:
            return []
        items = self.store.list_artifacts(session_id=session["id"], limit=20)
        return [
            {"id": item["id"], "type": item["artifact_type"], "summary": item.get("summary") or item.get("content", "")[:180]}
            for item in items
            if item.get("status") == "sealed"
        ][: max(0, min(2, int(limit)))]

    async def overview(self, scope: str = "") -> dict[str, Any]:
        key = self._scope(scope)
        state = await self._state(key)
        session = await asyncio.to_thread(self.store.get_active_session, key)
        now = self._now_local()
        stats = await asyncio.to_thread(self.store.stats, key)
        latest = await asyncio.to_thread(self.store.list_artifacts, key, "", "", "", 12)
        return {
            "version": "1", "enabled": bool(self.settings["house_enable"]),
            "natural_link": bool(self.settings["house_natural_link_enable"]),
            "configured": bool(self.settings["house_api_base_url"] and self.settings["house_model"] and self._api_key),
            "show_envelope": bool(self.settings["house_enable"] or self.settings["house_show_envelope_when_disabled"]),
            "scope_key": key, "current_time": now.isoformat(), "timezone": str(self.settings["house_time_zone"]),
            "period": self._period(now.hour), "stats": stats, "session": session,
            "environment": {
                "schema": 2,
                "period_source": "server_current_time",
                "background_variants": ["dawn", "day", "dusk", "night"],
                "crossfade": True,
                "character_light_match": True,
                "foreground_depth": True,
            },
            "consciousness": state.get("consciousness", "awake"),
            "motion": {
                "enabled": bool(self.settings["house_motion_enable"]),
                "intensity": str(self.settings["house_motion_intensity"]),
                "character_render_mode": str(self.settings["house_character_render_mode"]),
                "ambient": bool(self.settings["house_ambient_enable"]),
                "parallax": bool(self.settings["house_parallax_enable"]),
            },
            "character": self._character_scene(key, now, state, latest, session),
            "latest": latest[:5], "runtime": dict(self._health),
        }

    def _character_scene(
        self,
        scope_key: str,
        now: datetime,
        state: dict[str, Any],
        latest: list[dict[str, Any]],
        session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        period = self._period(now.hour)
        newest = latest[0] if latest else {}
        newest_type = str(newest.get("artifact_type") or "")
        newest_id = str(newest.get("id") or "")
        artifact_drives = newest.get("drive_candidates") or []
        if not isinstance(artifact_drives, list):
            artifact_drives = []
        artifact_drives = [row for row in artifact_drives if isinstance(row, dict)]
        fatigue = _clamp(state.get("fatigue"), 0, 0.3, 0)
        consciousness = str(state.get("consciousness") or "awake")
        try:
            drive_rows = top_drives(state, 5)
        except (KeyError, TypeError, ValueError):
            drive_rows = []
        decision = select_scene(
            scope_key=scope_key,
            now=now,
            variants=_CHARACTER_VARIANTS,
            top_drive_rows=drive_rows,
            fatigue=fatigue,
            consciousness=consciousness,
            newest_type=newest_type,
            artifact_drive_rows=artifact_drives,
        )
        signature = (
            decision["period"], decision["slot"], decision["fatigue_band"],
            consciousness, newest_type, newest_id, str((session or {}).get("id") or ""),
        )
        cached = self._character_scene_cache.get(scope_key)
        if cached and cached[0] == signature:
            return copy.deepcopy(cached[1])

        sprite = str(decision["sprite"])
        variant = _CHARACTER_VARIANTS[sprite]
        seed = hashlib.sha256(f"{scope_key}|{decision['slot']}|{sprite}|placement".encode("utf-8")).digest()
        placement_ids = tuple(str(item) for item in variant.get("placement_ids", ("balanced",)))
        compatible_placements = tuple(
            _CHARACTER_PLACEMENT_BY_ID[item]
            for item in placement_ids
            if item in _CHARACTER_PLACEMENT_BY_ID
        ) or (_CHARACTER_PLACEMENT_BY_ID["balanced"],)
        placement = compatible_placements[int.from_bytes(seed[4:8], "big") % len(compatible_placements)]
        x = max(8.0, min(92.0, float(variant["x"]) + float(placement["dx"])))
        y = max(12.0, min(82.0, float(variant["y"]) + float(placement["dy"])))
        scale = round(float(variant["scale"]) * float(placement["scale"]), 3)
        profiles = motion_catalog(
            sprite,
            period=period,
            fatigue=str(decision["fatigue_band"]),
            newest_type=newest_type,
            top_drive_rows=drive_rows,
        )
        reasons = [f"period:{period}", f"fatigue:{decision['fatigue_band']}"]
        if newest_type:
            reasons.append(f"artifact:{newest_type}")
        if decision["dominant_drives"]:
            reasons.append("drives:" + ",".join(str(row["key"]) for row in decision["dominant_drives"]))
        if consciousness == "sleeping":
            reasons.append("consciousness:sleeping")
        scene = {
            "variant_id": f"house-character-{sprite}-v1",
            "atlas": variant["atlas"], "sprite": sprite,
            "location": variant["location"], "pose": variant["pose"],
            "expression": variant["expression"], "outfit": variant["outfit"],
            "position": {
                "x": x, "y": y, "scale": scale,
            },
            "placement_variant": placement["id"],
            "prop_mode": str(variant.get("prop_mode") or "free"),
            "compatible_placements": [str(item["id"]) for item in compatible_placements],
            "idle_animation": profiles[0]["primitive"] if profiles else _CHARACTER_IDLE_ANIMATIONS.get(sprite, "breathe"),
            "idle_motion_id": profiles[0]["id"] if profiles else "",
            "reaction_animation": _CHARACTER_REACTION_ANIMATIONS.get(str(variant["expression"]), "nod"),
            "motion_profiles": profiles,
            "motion_scheduler": {
                "check_seconds": [8, 20],
                "still_seconds": [2, 10],
                "max_duty_cycle": 0.25,
                "max_consecutive": 3,
                "profile_count": len(profiles),
            },
            "selection": {
                "schema": 2,
                "period_source": "server_current_time",
                "hold_minutes": decision["hold_minutes"],
                "slot": decision["slot"],
                "score": decision["score"],
                "fatigue_band": decision["fatigue_band"],
                "dominant_drives": decision["dominant_drives"],
                "newest_artifact_type": newest_type,
                "newest_artifact_id": newest_id,
                "artifact_drives": decision["artifact_drives"],
                "components": decision["components"],
                "alternatives": decision["alternatives"],
            },
            "state_reasons": reasons,
            "available_variants": len(_CHARACTER_VARIANTS),
            "available_scene_combinations": sum(
                len(tuple(item.get("placement_ids", ("balanced",))))
                for item in _CHARACTER_VARIANTS.values()
            ),
            "available_motion_profiles": sum(len(items) for items in MOTION_PROFILES.values()),
            "meaningful_state_combinations": sum(
                len(tuple(item.get("placement_ids", ("balanced",))))
                * len(MOTION_PROFILES.get(name, ()))
                for name, item in _CHARACTER_VARIANTS.items()
            ),
            "interactive": True, "interaction_api": "/api/house/character/interact",
            "interaction_schema": 2,
            "rig": {
                "schema": 2,
                "profile": sprite,
                "renderer": "webgl_mesh2d",
                "channels": [
                    "head", "hair", "torso", "left_sleeve",
                    "right_sleeve", "skirt", "prop", "face",
                    "blink", "gaze", "mouth",
                ],
                "fallback": "classic_sprite",
            },
            "session_id": str((session or {}).get("id") or ""),
        }
        if len(self._character_scene_cache) >= 256 and scope_key not in self._character_scene_cache:
            self._character_scene_cache.pop(next(iter(self._character_scene_cache)))
        self._character_scene_cache[scope_key] = (signature, copy.deepcopy(scene))
        return scene

    async def artifacts(self, scope: str = "", artifact_type: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.store.list_artifacts, self._scope(scope), "", artifact_type, "", limit,
        )

    async def dreams(self, scope: str = "") -> list[dict[str, Any]]:
        state = await self._state(self._scope(scope))
        result = []
        for item in reversed(list(state.get("recentDreams") or [])[-20:]):
            if not isinstance(item, dict):
                continue
            result.append({
                "id": str(item.get("id") or ""),
                "created_at": item.get("createdAt") or item.get("at") or "",
                "residue": _compact(item.get("residue"), 500),
                "awareness": _compact(item.get("awareness"), 500),
                "reality_status": "dream",
                "source": "xinchao",
            })
        return result

    async def artifact_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(body.get("artifact_id") or "")
        artifact = await asyncio.to_thread(self.store.get_artifact, artifact_id)
        if not artifact:
            raise KeyError("artifact not found")
        if action == "read":
            return {"artifact": await asyncio.to_thread(self.store.update_artifact, artifact_id, is_read=True)}
        if action == "archive":
            return {"artifact": await asyncio.to_thread(self.store.update_artifact, artifact_id, status="archived")}
        if action == "delete":
            return {"deleted": await asyncio.to_thread(self.store.delete_artifact, artifact_id)}
        if action == "feedback":
            kind = str(body.get("feedback_type") or "")
            if kind not in _FEEDBACK_TYPES:
                raise ValueError("invalid feedback_type")
            item = await asyncio.to_thread(
                self.store.add_feedback, artifact_id, artifact["scope_key"], kind, str(body.get("note") or ""),
            )
            return {"feedback": item}
        raise ValueError("unsupported artifact action")

    async def send_artifact(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.get("house_send_enable"):
            raise ValueError("house delivery is disabled")
        artifact_id = str(body.get("artifact_id") or "")
        artifact = await asyncio.to_thread(self.store.get_artifact, artifact_id)
        if not artifact or not artifact.get("sendable") or artifact.get("status") not in {"sealed", "archived"}:
            raise ValueError("artifact is not sendable")
        revision = int(body.get("revision") or 0)
        if revision != int(artifact.get("revision") or 1):
            raise ValueError("artifact revision changed")
        key = str(body.get("idempotency_key") or "")[:160]
        if len(key) < 8:
            raise ValueError("idempotency_key is required")
        existing = await asyncio.to_thread(self.store.delivery_by_key, key)
        if existing and existing.get("status") in {"sent", "pending"}:
            return {"delivery": existing, "idempotent": True}
        recent = await asyncio.to_thread(self.store.recent_deliveries, artifact["scope_key"], 20)
        cooldown = float(self.settings["house_send_cooldown_hours"]) * 3600
        now = datetime.now(timezone.utc)
        for item in recent:
            sent = _parse_iso(item.get("sent_at"))
            if item.get("status") == "sent" and sent and (now - sent).total_seconds() < cooldown:
                raise ValueError("delivery cooldown is active")
        session = next((x for x in self.store.list_sessions(artifact["scope_key"], 20) if x["id"] == artifact["session_id"]), {})
        umo = str(session.get("umo") or "")
        if not umo:
            raise ValueError("no verified conversation target")
        delivery = await asyncio.to_thread(self.store.create_delivery, artifact, umo, key)
        try:
            await self.plugin.context.send_message(umo, MessageChain().message(str(artifact["content"])))
            delivery = await asyncio.to_thread(self.store.finish_delivery, delivery["id"], "sent", "")
            await asyncio.to_thread(self.store.update_artifact, artifact_id, status="sent")
            await asyncio.to_thread(self.store.consume_many, [artifact_id], "delivery", delivery["id"], _hash(artifact["content"]))
            logger.info("[memos-memory][house] artifact sent id=%s delivery=%s", artifact_id, delivery["id"])
            return {"delivery": delivery, "idempotent": False}
        except Exception as exc:
            await asyncio.to_thread(self.store.finish_delivery, delivery["id"], "failed", str(exc))
            raise

    async def character_interact(self, body: dict[str, Any]) -> dict[str, Any]:
        overview = await self.overview(str(body.get("scope") or ""))
        character = overview["character"]
        action = str(body.get("action") or "tap")[:32]
        now = self._now_local()
        recent = await asyncio.to_thread(self.store.recent_character_interactions, overview["scope_key"], 20)
        reaction_policy = select_reaction(
            str(character.get("expression") or "gentle"), recent,
            now=now,
            seed=f"{overview['scope_key']}|{character['sprite']}|{action}|{int(now.timestamp() // 30)}",
        )
        animation = str(reaction_policy["animation"])
        reaction_key = f"{overview['period']}:{character['sprite']}:{action}:{animation}"
        seed = int(hashlib.sha256((overview["scope_key"] + reaction_key + str(int(time.time() // 30))).encode()).hexdigest()[:8], 16)
        reactions = [
            *_CHARACTER_REACTIONS.get(character["expression"], []),
            *_CHARACTER_LOCATION_REACTIONS.get(character["location"], []),
            *_CHARACTER_PERIOD_REACTIONS[overview["period"]],
        ]
        text = random.Random(seed).choice(reactions)
        event = await asyncio.to_thread(
            self.store.record_character_interaction,
            scope_key=overview["scope_key"], session_id=character.get("session_id", ""),
            scene_period=overview["period"], location=character["location"], pose=character["pose"],
            expression=character["expression"], variant_id=character["variant_id"],
            outfit=character["outfit"], action=action, reaction_key=reaction_key,
        )
        return {
            "interaction": event, "reaction": {"text": text, "key": reaction_key, "source": "local"},
            "character": character,
            "extension": {
                "schema": 2,
                "supports_llm": False,
                "supports_animation": True,
                "accepted_actions": ["tap"],
                "suggested_animation": animation,
                "cooldown": reaction_policy,
                "future_hooks": ["reaction_provider", "animation_driver"],
            },
        }

    async def test_connection(self) -> dict[str, Any]:
        if not (self._chat_url() and self._api_key and self.settings.get("house_model")):
            return {"ok": False, "reason": "incomplete_configuration"}
        snapshot = {
            "meta": {"current_time": self._now_local().isoformat(), "idle_hours": 8, "character": "测试角色"},
            "xinchao": {}, "body": {}, "rolling_state": {}, "profile": {},
            "prospective_items": [], "memory_evidence": [],
        }
        parsed = await self._call_model(snapshot, "whisper")
        return {"ok": isinstance(parsed, dict), "runtime": dict(self._health)}

    async def settle_now(self, scope: str = "") -> dict[str, Any]:
        return await self._settle_scope(self._scope(scope), force=True)
