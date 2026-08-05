from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context
from astrbot.core.agent.message import TextPart

from .time_insight_engine import (
    EngineConfig,
    build_candidates,
    clean_text,
    flatten_evidence,
    render_query_block,
    render_static_block,
    select_query_candidates,
    select_static_candidates,
)


_ENGINE_VERSION = "3.0.0-integrated"
_SCHEMA_VERSION = 3


def _bounded_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return copy.deepcopy(default)


class IntegratedTimeInsightService:
    """Evidence-bound temporal resonance owned by the main memory plugin."""

    def __init__(self, host: Any):
        self.host = host
        self.context: Context = host.context
        config = self._translated_config(host)
        self.config = config
        old_auto_days = _bounded_int(config.get("auto_update_days", 1), 0, 90, 1)
        self.enable = bool(config.get("enable", True))
        self.vec_db_path = str(
            config.get("vec_db_path", "./data/astrbot_plugin_memos_memory/memories.db")
        ).strip()
        self.timezone = str(config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
        try:
            ZoneInfo(self.timezone)
        except Exception:
            self.timezone = "Asia/Shanghai"
        self.auto_update_hours = _bounded_float(
            config.get("auto_update_hours", old_auto_days * 24), 0, 720, 12
        )
        self.source_memo_limit = _bounded_int(config.get("source_memo_limit", 5000), 100, 50000, 5000)
        self.query_scoped_enable = bool(config.get("query_scoped_enable", True))
        self.query_lookup_timeout = _bounded_float(config.get("query_lookup_timeout", 0.8), 0.1, 5, 0.8)
        self.llm_refine_enable = bool(config.get("llm_refine_enable", True))
        self.llm_provider_id = str(config.get("llm_provider_id", "") or "").strip()
        self.llm_timeout = _bounded_float(config.get("llm_timeout", 45), 5, 180, 45)
        self.llm_min_confidence = _bounded_float(
            config.get("llm_min_confidence", 0.72), 0.5, 0.95, 0.72
        )
        self.diagnostic_log = bool(config.get("diagnostic_log", True))
        self.engine_config = EngineConfig(
            timezone=self.timezone,
            recent_window_days=_bounded_int(
                config.get("recent_window_days", config.get("recent_days", 21)), 3, 120, 21
            ),
            anniversary_window_days=_bounded_int(
                config.get("anniversary_window_days", 2), 0, 14, 2
            ),
            min_importance=_bounded_int(config.get("min_importance", 3), 1, 5, 3),
            min_evidence_score=_bounded_float(
                config.get("min_evidence_score", 0.68), 0.45, 0.95, 0.68
            ),
            exact_anniversary_limit=_bounded_int(
                config.get("exact_anniversary_limit", 3), 0, 10, 3
            ),
            nearby_anniversary_limit=_bounded_int(
                config.get("nearby_anniversary_limit", 2), 0, 8, 2
            ),
            trend_min_distinct_days=_bounded_int(
                config.get("trend_min_distinct_days", 3), 2, 14, 3
            ),
            trend_min_evidence=_bounded_int(config.get("trend_min_evidence", 3), 2, 20, 3),
            seasonal_min_years=_bounded_int(config.get("seasonal_min_years", 3), 2, 10, 3),
            static_max_insights=_bounded_int(config.get("static_max_insights", 1), 0, 4, 1),
            injection_max_chars=_bounded_int(
                config.get("injection_max_chars", 800), 200, 4000, 800
            ),
            query_max_insights=_bounded_int(config.get("query_max_insights", 2), 0, 6, 2),
            query_min_score=_bounded_float(config.get("query_min_score", 0.42), 0.2, 0.9, 0.42),
            query_injection_max_chars=_bounded_int(
                config.get("query_injection_max_chars", 900), 200, 3000, 900
            ),
        )
        self._auto_task: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()
        self._update_lock = asyncio.Lock()
        self._last_umo = ""
        self._last_schedule_check = 0.0
        self._stopping = False
        self.repeat_cooldown_minutes = _bounded_int(
            config.get("repeat_cooldown_minutes", 180), 0, 1440, 180
        )
        self._ambient_sent: dict[str, tuple[str, float]] = {}
        if self.diagnostic_log:
            logger.info(
                "[memos-insight] v%s loaded | db=%s | timezone=%s | query=%s | llm_refine=%s",
                _ENGINE_VERSION,
                self.vec_db_path,
                self.timezone,
                self.query_scoped_enable,
                self.llm_refine_enable,
            )

    @staticmethod
    def _translated_config(host: Any) -> dict[str, Any]:
        """Map main-plugin settings to the former affiliate engine names."""
        source = getattr(host, "config", {}) or {}
        def value(name: str, default: Any) -> Any:
            return source.get("time_insight_" + name, default)
        return {
            "enable": source.get("enable_time_insight_affiliate", True),
            "vec_db_path": getattr(host, "vec_db_path", "./data/astrbot_plugin_memos_memory/memories.db"),
            "timezone": getattr(host, "rp_time_timezone", "Asia/Shanghai"),
            "auto_update_hours": value("auto_update_hours", 12),
            "source_memo_limit": value("source_memo_limit", 5000),
            "recent_window_days": value("recent_window_days", 21),
            "anniversary_window_days": value("anniversary_window_days", 2),
            "min_importance": value("min_importance", 3),
            "min_evidence_score": value("min_evidence_score", 0.68),
            "exact_anniversary_limit": value("exact_anniversary_limit", 3),
            "nearby_anniversary_limit": value("nearby_anniversary_limit", 2),
            "trend_min_distinct_days": value("trend_min_distinct_days", 3),
            "trend_min_evidence": value("trend_min_evidence", 3),
            "seasonal_min_years": value("seasonal_min_years", 3),
            "static_max_insights": value("ambient_max_insights", 1),
            "injection_max_chars": value("ambient_max_chars", 620),
            "query_scoped_enable": value("query_enable", True),
            "query_max_insights": value("query_max_insights", 2),
            "query_min_score": value("query_min_score", 0.42),
            "query_injection_max_chars": value("query_max_chars", 900),
            "query_lookup_timeout": value("query_lookup_timeout", 0.8),
            "llm_refine_enable": value("llm_refine_enable", True),
            "llm_provider_id": value("llm_provider_id", ""),
            "llm_timeout": value("llm_timeout", 45),
            "llm_min_confidence": value("llm_min_confidence", 0.72),
            "diagnostic_log": value("diagnostic_log", True),
            "repeat_cooldown_minutes": value("repeat_cooldown_minutes", 180),
        }

    def apply_settings(self) -> None:
        """Rebuild the bounded runtime settings after a WebUI hot save."""
        refreshed = IntegratedTimeInsightService(self.host)
        preserved = {
            "_auto_task": self._auto_task,
            "_jobs": self._jobs,
            "_update_lock": self._update_lock,
            "_last_umo": self._last_umo,
            "_last_schedule_check": self._last_schedule_check,
            "_stopping": self._stopping,
            "_ambient_sent": self._ambient_sent,
        }
        for name, value in vars(refreshed).items():
            if name not in preserved:
                setattr(self, name, value)
        for name, value in preserved.items():
            setattr(self, name, value)

    def external_affiliate_active(self) -> bool:
        """Yield to the old companion plugin so existing installs never double inject."""
        try:
            metadata = self.context.get_registered_star("astrbot_plugin_memos_memory_insight")
            return bool(metadata and getattr(metadata, "activated", False))
        except Exception:
            return False

    @property
    def owns_injection(self) -> bool:
        return bool(self.enable and not self.external_affiliate_active())

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))

    def _db_file(self) -> Path:
        return Path(self.vec_db_path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        path = self._db_file()
        if not path.is_file():
            raise FileNotFoundError(
                f"主插件数据库不存在: {path}。请确认 vec_db_path 与 memos-memory 完全一致"
            )
        conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema(conn)
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        if not IntegratedTimeInsightService._table_exists(conn, table):
            return set()
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, ddl: str,
    ) -> None:
        if column not in IntegratedTimeInsightService._columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_time_insights (
                id INTEGER PRIMARY KEY CHECK (id=1),
                updated_ts REAL,
                injection_block TEXT,
                evidence_json TEXT,
                stats_json TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_insight_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL,
                status TEXT,
                message TEXT
            )"""
        )
        self._ensure_column(conn, "memory_insight_runs", "detail_json", "TEXT DEFAULT '{}'")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_time_insight_candidates (
                candidate_id TEXT PRIMARY KEY,
                generated_ts REAL NOT NULL,
                kind TEXT NOT NULL,
                score REAL DEFAULT 0,
                confidence REAL DEFAULT 0,
                title TEXT DEFAULT '',
                claim TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '[]',
                terms_json TEXT DEFAULT '[]',
                static_selected INTEGER DEFAULT 0,
                synthesis TEXT DEFAULT 'deterministic'
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_time_insight_runtime (
                scope_key TEXT PRIMARY KEY,
                updated_ts REAL,
                query_hash TEXT,
                injection_block TEXT,
                evidence_json TEXT,
                stats_json TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_insight_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_time_insight_kind "
            "ON memory_time_insight_candidates(kind, score DESC)"
        )
        conn.commit()

    async def initialize(self) -> None:
        if not self.enable:
            return
        if self.external_affiliate_active():
            logger.warning(
                "[memos-memory][time-insight] 检测到旧附属插件仍在运行，内置引擎已自动让出，避免重复注入"
            )
            return
        self._stopping = False
        try:
            await asyncio.to_thread(self._invalidate_legacy_output_sync)
        except FileNotFoundError:
            logger.warning(
                "[memos-insight] 等待主插件数据库，当前不会创建空 memories.db: %s",
                self.vec_db_path,
            )
        except Exception as exc:
            logger.warning("[memos-insight] legacy output check failed open: %s", exc)
        self._auto_task = asyncio.create_task(
            self._auto_loop(), name="memos-insight-auto-update"
        )
        self._spawn_update_if_needed("startup")

    async def terminate(self) -> None:
        self._stopping = True
        tasks = [task for task in [self._auto_task, *self._jobs] if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._auto_task = None
        self._jobs.clear()

    async def _auto_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(600)
                self._spawn_update_if_needed("auto")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[memos-insight] auto loop failed open: %s", exc)

    def _spawn(self, awaitable: Any, name: str) -> None:
        task = asyncio.create_task(awaitable, name=name)
        self._jobs.add(task)

        def done(finished: asyncio.Task) -> None:
            self._jobs.discard(finished)
            if finished.cancelled():
                return
            try:
                exc = finished.exception()
            except Exception:
                return
            if exc is not None:
                logger.warning("[memos-insight] background task failed: %s", exc)

        task.add_done_callback(done)

    def _spawn_update_if_needed(self, reason: str) -> None:
        if (
            self._stopping
            or not self.enable
            or self.auto_update_hours <= 0
            or self.external_affiliate_active()
        ):
            return
        now_mono = time.monotonic()
        if now_mono - self._last_schedule_check < 30:
            return
        self._last_schedule_check = now_mono
        self._spawn(self._update_if_needed(reason), "memos-insight-update-check")

    async def _update_if_needed(self, reason: str) -> None:
        if self.external_affiliate_active():
            return
        try:
            needs_update = await asyncio.to_thread(self._needs_update_sync)
        except Exception:
            return
        if needs_update and not self._update_lock.locked():
            await self._update(reason, self._last_umo)

    def _invalidate_legacy_output_sync(self) -> None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT stats_json FROM memory_time_insights WHERE id=1"
            ).fetchone()
            stats = _json_loads(row["stats_json"], {}) if row else {}
            if row and int(stats.get("engine_version") or 0) < _SCHEMA_VERSION:
                conn.execute(
                    "UPDATE memory_time_insights SET injection_block='' WHERE id=1"
                )
                conn.commit()
        finally:
            conn.close()

    def _provider(self, umo: str):
        try:
            if self.llm_provider_id:
                return self.context.get_provider_by_id(self.llm_provider_id)
            if umo:
                return self.context.get_using_provider(umo)
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        source = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I).strip()
        fence = re.escape(chr(96) * 3)
        candidates = [
            match.group(1).strip()
            for match in re.finditer(
                fence + r"(?:json)?\s*([\s\S]*?)" + fence,
                source,
                re.I,
            )
        ]
        candidates.append(source)
        decoder = json.JSONDecoder(strict=False)
        for candidate in candidates:
            for start in [index for index, char in enumerate(candidate) if char == "{"]:
                fragment = candidate[start:].strip()
                try:
                    parsed, _ = decoder.raw_decode(fragment)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                repaired = re.sub(r",\s*([}\]])", r"\1", fragment)
                try:
                    parsed = ast.literal_eval(repaired)
                    if isinstance(parsed, dict):
                        return parsed
                except (SyntaxError, ValueError):
                    pass
        return None

    async def _refine_candidates(
        self, candidates: list[dict[str, Any]], umo: str,
    ) -> dict[str, dict[str, Any]]:
        if not self.llm_refine_enable or not candidates:
            return {}
        provider = self._provider(umo)
        if provider is None:
            return {}
        payload = []
        for candidate in candidates[:12]:
            payload.append({
                "candidate_id": candidate["candidate_id"],
                "kind": candidate["kind"],
                "deterministic_claim": candidate["claim"],
                "score": candidate["score"],
                "mixed": candidate["mixed"],
                "evidence": [
                    {
                        "id": item["id"],
                        "date": item["date"],
                        "type": item["type"],
                        "text": item["text"],
                    }
                    for item in candidate["evidence"][:5]
                ],
            })
        prompt = f"""你是长期角色记忆的时间证据审校器。你只能精炼已经由确定性算法建立的候选，不能创造新事件、日期、人物、关系或因果。

当前日期：{self._now().date().isoformat()}
候选证据：
{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}

输出严格 JSON：
{{
  "insights": [
    {{
      "candidate_id": "原候选ID",
      "evidence_ids": ["实际使用的证据ID"],
      "claim": "不超过180字的心理时间解释",
      "confidence": 0.0
    }}
  ]
}}

规则：
1. 每条 claim 必须完全由 evidence_ids 支持；不得补写证据里没有的事实。
2. 日期相近只说明可能唤起联想，不等于今天重演，不得写成命运、因果或必然。
3. mixed=true 时必须保留“方向并存、仍在变化”的含义，不能总结成单向关系。
4. confidence 衡量证据支持度，不是情绪强度；证据不足可不返回该候选。
5. 不输出 Markdown、推理过程或额外说明。
"""
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是证据约束的时间记忆审校器，只输出一个合法 JSON 对象。",
            ),
            timeout=self.llm_timeout,
        )
        parsed = self._parse_json(getattr(response, "completion_text", "") or "")
        if not parsed or not isinstance(parsed.get("insights"), list):
            raise ValueError("LLM 返回缺少 insights JSON 数组")
        known = {candidate["candidate_id"]: candidate for candidate in candidates}
        refined: dict[str, dict[str, Any]] = {}
        for item in parsed["insights"][:12]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id") or "")
            candidate = known.get(candidate_id)
            if candidate is None:
                continue
            allowed_ids = set(candidate["evidence_ids"])
            evidence_ids = [
                str(value) for value in list(item.get("evidence_ids") or [])
                if str(value) in allowed_ids
            ]
            if not evidence_ids:
                continue
            confidence = _bounded_float(item.get("confidence"), 0, 1, 0)
            if confidence < self.llm_min_confidence:
                continue
            claim = clean_text(item.get("claim"), 180)
            if not claim:
                continue
            allowed_dates = {
                evidence["date"] for evidence in candidate["evidence"]
            } | {self._now().date().isoformat()}
            dates_in_claim = set(re.findall(r"(?:19|20)\d{2}-\d{2}-\d{2}", claim))
            if dates_in_claim - allowed_dates:
                continue
            if candidate.get("mixed") and not any(
                marker in claim for marker in ("并存", "反复", "不一致", "仍在变化", "双向")
            ):
                continue
            refined[candidate_id] = {
                "claim": claim,
                "confidence": confidence,
                "evidence_ids": evidence_ids,
            }
        return refined

    @staticmethod
    def _apply_refinements(
        candidates: list[dict[str, Any]],
        refinements: dict[str, dict[str, Any]],
    ) -> int:
        applied = 0
        for candidate in candidates:
            refined = refinements.get(candidate["candidate_id"])
            if not refined:
                continue
            candidate["claim"] = refined["claim"]
            candidate["confidence"] = round(min(
                float(candidate.get("confidence") or 0),
                float(refined.get("confidence") or 0),
            ), 4)
            candidate["model_confidence"] = refined["confidence"]
            candidate["llm_evidence_ids"] = refined["evidence_ids"]
            candidate["synthesis"] = "llm_refined"
            applied += 1
        return applied

    def _write_result_sync(
        self,
        candidates: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        block: str,
        stats: dict[str, Any],
        reason: str,
    ) -> None:
        conn = self._connect()
        now_ts = time.time()
        evidence = flatten_evidence(selected)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM memory_time_insight_candidates")
            for candidate in candidates:
                conn.execute(
                    """INSERT INTO memory_time_insight_candidates
                       (candidate_id, generated_ts, kind, score, confidence, title, claim,
                        evidence_json, terms_json, static_selected, synthesis)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate["candidate_id"],
                        now_ts,
                        candidate["kind"],
                        candidate["score"],
                        candidate["confidence"],
                        candidate["title"],
                        candidate["claim"],
                        json.dumps(candidate["evidence"], ensure_ascii=False),
                        json.dumps(candidate["terms"], ensure_ascii=False),
                        1 if candidate.get("static_selected") else 0,
                        candidate.get("synthesis", "deterministic"),
                    ),
                )
            conn.execute(
                """INSERT OR REPLACE INTO memory_time_insights
                   (id, updated_ts, injection_block, evidence_json, stats_json)
                   VALUES (1,?,?,?,?)""",
                (
                    now_ts,
                    block,
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(stats, ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO memory_insight_meta(key,value) VALUES ('schema_version',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(_SCHEMA_VERSION),),
            )
            conn.execute(
                """INSERT INTO memory_insight_runs(created_ts,status,message,detail_json)
                   VALUES (?,?,?,?)""",
                (
                    now_ts,
                    "ok",
                    f"{reason}: temporal insight v2 updated",
                    json.dumps(stats, ensure_ascii=False),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _record_failure_sync(self, reason: str, exc: Exception) -> None:
        try:
            conn = self._connect()
            conn.execute(
                """INSERT INTO memory_insight_runs(created_ts,status,message,detail_json)
                   VALUES (?,?,?,?)""",
                (
                    time.time(),
                    "failed",
                    f"{reason}: {str(exc)[:300]}",
                    json.dumps({"error_type": exc.__class__.__name__}, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    async def _update(self, reason: str, umo: str = "") -> dict[str, Any]:
        async with self._update_lock:
            try:
                memories = await asyncio.to_thread(self._load_memories_sync)
                now = self._now()
                candidates, stats = await asyncio.to_thread(
                    build_candidates, memories, now, self.engine_config
                )
                llm_status = "disabled"
                llm_applied = 0
                if self.llm_refine_enable and candidates:
                    try:
                        refinements = await self._refine_candidates(candidates, umo)
                        llm_applied = self._apply_refinements(candidates, refinements)
                        llm_status = "refined" if llm_applied else (
                            "no_provider" if self._provider(umo) is None else "no_valid_refinement"
                        )
                    except asyncio.TimeoutError:
                        llm_status = "timeout_fallback"
                    except Exception as exc:
                        llm_status = "invalid_fallback"
                        logger.warning(
                            "[memos-insight] LLM 精炼失败，保留确定性洞察: %s", exc
                        )
                selected = select_static_candidates(candidates, self.engine_config)
                block = render_static_block(selected, now, self.engine_config)
                stats.update({
                    "engine_version": _SCHEMA_VERSION,
                    "updated_reason": reason,
                    "static_selected": len(selected),
                    "static_chars": len(block),
                    "llm_status": llm_status,
                    "llm_applied": llm_applied,
                    "query_scoped_enable": self.query_scoped_enable,
                    "settings": {
                        "recent_window_days": self.engine_config.recent_window_days,
                        "anniversary_window_days": self.engine_config.anniversary_window_days,
                        "min_evidence_score": self.engine_config.min_evidence_score,
                        "trend_min_distinct_days": self.engine_config.trend_min_distinct_days,
                        "seasonal_min_years": self.engine_config.seasonal_min_years,
                    },
                })
                await asyncio.to_thread(
                    self._write_result_sync,
                    candidates,
                    selected,
                    block,
                    stats,
                    reason,
                )
                if self.diagnostic_log:
                    logger.info(
                        "[memos-insight] update=%s source=%d dated=%d candidates=%d "
                        "static=%d chars=%d llm=%s",
                        reason,
                        stats["source_memories"],
                        stats["dated_memories"],
                        stats["candidate_count"],
                        len(selected),
                        len(block),
                        llm_status,
                    )
                return stats
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.to_thread(self._record_failure_sync, reason, exc)
                raise

    def _load_candidates_sync(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._connect()
        try:
            status_row = conn.execute(
                "SELECT stats_json FROM memory_time_insights WHERE id=1"
            ).fetchone()
            stats = _json_loads(status_row["stats_json"], {}) if status_row else {}
            if (
                int(stats.get("engine_version") or 0) != _SCHEMA_VERSION
                or str(stats.get("generated_date") or "") != self._now().date().isoformat()
            ):
                return [], stats
            rows = conn.execute(
                """SELECT candidate_id, kind, score, confidence, title, claim,
                          evidence_json, terms_json, static_selected, synthesis
                   FROM memory_time_insight_candidates
                   ORDER BY score DESC"""
            ).fetchall()
            candidates = []
            for row in rows:
                item = dict(row)
                item["evidence"] = _json_loads(item.pop("evidence_json"), [])
                item["terms"] = _json_loads(item.pop("terms_json"), [])
                item["static_selected"] = bool(item["static_selected"])
                candidates.append(item)
            return candidates, stats
        finally:
            conn.close()

    def _write_runtime_sync(
        self,
        scope_key: str,
        query: str,
        block: str,
        selected: list[dict[str, Any]],
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO memory_time_insight_runtime
                   (scope_key,updated_ts,query_hash,injection_block,evidence_json,stats_json)
                   VALUES (?,?,?,?,?,?)""",
                (
                    scope_key[:500],
                    time.time(),
                    hashlib.sha256(query.encode("utf-8")).hexdigest()[:20],
                    block,
                    json.dumps(flatten_evidence(selected), ensure_ascii=False),
                    json.dumps({
                        "selected": len(selected),
                        "candidate_ids": [item["candidate_id"] for item in selected],
                        "query_scores": [item.get("query_score", 0) for item in selected],
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _explicit_temporal_query(query: str) -> bool:
        return bool(
            re.search(r"(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}日", query)
            or any(marker in query for marker in (
                "以前", "过去", "那天", "当时", "去年", "前年", "往年", "纪念日",
                "周年", "最近", "这段时间", "一直", "每年", "还记得", "什么时候",
            ))
        )

    def _allow_ambient(self, scope_key: str, candidates: list[dict[str, Any]], query: str) -> bool:
        if not candidates:
            return False
        fingerprint = "|".join(sorted(str(item.get("candidate_id") or "") for item in candidates))
        previous = self._ambient_sent.get(scope_key)
        if (
            previous
            and previous[0] == fingerprint
            and self.repeat_cooldown_minutes > 0
            and time.time() - previous[1] < self.repeat_cooldown_minutes * 60
            and not self._explicit_temporal_query(query)
        ):
            return False
        self._ambient_sent[scope_key] = (fingerprint, time.time())
        return True

    @staticmethod
    def move_injection_last(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list) or len(parts) < 2:
            return
        matched = []
        remaining = []
        for part in parts:
            text = str(getattr(part, "text", "") or "")
            (matched if "<IntegratedHistoricalTimeInsight" in text else remaining).append(part)
        if matched:
            parts[:] = remaining + matched

    async def on_request(self, event: AstrMessageEvent, req: ProviderRequest) -> dict[str, Any]:
        result = {"chars": 0, "selected": 0, "ambient": 0, "query": 0, "mode": "disabled"}
        if not self.enable:
            return result
        if self.external_affiliate_active():
            result["mode"] = "external_affiliate"
            return result
        if not self._db_file().is_file():
            result["mode"] = "waiting_for_memory_db"
            return result
        result["mode"] = "integrated"
        self._last_umo = str(getattr(event, "unified_msg_origin", "") or "")
        self._spawn_update_if_needed("request")
        query = clean_text(getattr(event, "message_str", ""), 2400)
        if not query:
            return result
        try:
            candidates, _ = await asyncio.wait_for(
                asyncio.to_thread(self._load_candidates_sync),
                timeout=self.query_lookup_timeout,
            )
            query_selected = (
                select_query_candidates(query, candidates, self.engine_config)
                if self.query_scoped_enable and self.engine_config.query_max_insights > 0
                else []
            )
            query_ids = {str(item.get("candidate_id") or "") for item in query_selected}
            ambient = [
                item for item in candidates
                if item.get("static_selected") and str(item.get("candidate_id") or "") not in query_ids
            ]
            scope_key = self._last_umo or "default"
            if not self._allow_ambient(scope_key, ambient, query):
                ambient = []
            now = self._now()
            ambient_block = render_static_block(ambient, now, self.engine_config)
            query_block = render_query_block(query_selected, now, self.engine_config)
            sections = []
            if ambient_block:
                sections.append("[今日环境回声]\n" + ambient_block)
            if query_block:
                sections.append(query_block)
            if not sections:
                return result
            block = (
                f'<IntegratedHistoricalTimeInsight current_date="{now.date().isoformat()}" '
                'temporal_role="historical_evidence">\n'
                "这是有日期证据的历史联想层，不是当前事件，也不能覆盖本轮对话事实。\n"
                + "\n\n".join(sections)
                + "\n</IntegratedHistoricalTimeInsight>"
            )
            selected = ambient + query_selected
            if not block:
                return result
            if getattr(req, "extra_user_content_parts", None) is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            self.move_injection_last(req)
            result.update({
                "chars": len(block),
                "selected": len(selected),
                "ambient": len(ambient),
                "query": len(query_selected),
                "candidate_ids": [item["candidate_id"] for item in selected],
                "kinds": [item["kind"] for item in selected],
            })
            try:
                event.set_extra("memos_time_insight_v3", result)
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._write_runtime_sync,
                        scope_key,
                        query,
                        block,
                        selected,
                    ),
                    timeout=self.query_lookup_timeout,
                )
            except Exception:
                pass
            if self.diagnostic_log:
                logger.info(
                    "[memos-memory][time-insight] 注入 %d 条，%d 字 | ambient=%d query=%d kinds=%s",
                    len(selected),
                    len(block),
                    len(ambient),
                    len(query_selected),
                    [item["kind"] for item in selected],
                )
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "[memos-memory][time-insight] 查询超时 %.2fs，已跳过",
                self.query_lookup_timeout,
            )
        except Exception as exc:
            logger.warning("[memos-memory][time-insight] 注入失败，已放行: %s", exc)
        return result

    def _status_sync(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT updated_ts,injection_block,evidence_json,stats_json
                   FROM memory_time_insights WHERE id=1"""
            ).fetchone()
            run = conn.execute(
                """SELECT created_ts,status,message FROM memory_insight_runs
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            candidate_count = int(conn.execute(
                "SELECT COUNT(*) AS n FROM memory_time_insight_candidates"
            ).fetchone()["n"] or 0)
            runtime = conn.execute(
                """SELECT scope_key,updated_ts,injection_block,stats_json
                   FROM memory_time_insight_runtime
                   ORDER BY updated_ts DESC LIMIT 1"""
            ).fetchone()
            candidate_rows = conn.execute(
                """SELECT candidate_id,kind,score,confidence,title,claim,
                          evidence_json,static_selected,synthesis
                   FROM memory_time_insight_candidates
                   ORDER BY static_selected DESC, score DESC LIMIT 16"""
            ).fetchall()
            candidate_preview = []
            for candidate_row in candidate_rows:
                item = dict(candidate_row)
                item["evidence"] = _json_loads(item.pop("evidence_json"), [])
                item["static_selected"] = bool(item.get("static_selected"))
                candidate_preview.append(item)
            if not row:
                return {
                    "generated": False,
                    "candidate_count": candidate_count,
                    "candidates": candidate_preview,
                }
            return {
                "generated": True,
                "updated_ts": float(row["updated_ts"] or 0),
                "age_hours": round((time.time() - float(row["updated_ts"] or 0)) / 3600, 2),
                "block": row["injection_block"] or "",
                "evidence_count": len(_json_loads(row["evidence_json"], [])),
                "stats": _json_loads(row["stats_json"], {}),
                "candidate_count": candidate_count,
                "candidates": candidate_preview,
                "last_run": dict(run) if run else None,
                "last_runtime": {
                    **dict(runtime),
                    "stats": _json_loads(runtime["stats_json"], {}),
                } if runtime else None,
            }
        finally:
            conn.close()

    def settings_snapshot(self) -> dict[str, Any]:
        return {
            "enable_time_insight_affiliate": self.enable,
            "time_insight_auto_update_hours": self.auto_update_hours,
            "time_insight_source_memo_limit": self.source_memo_limit,
            "time_insight_recent_window_days": self.engine_config.recent_window_days,
            "time_insight_anniversary_window_days": self.engine_config.anniversary_window_days,
            "time_insight_min_importance": self.engine_config.min_importance,
            "time_insight_min_evidence_score": self.engine_config.min_evidence_score,
            "time_insight_trend_min_distinct_days": self.engine_config.trend_min_distinct_days,
            "time_insight_trend_min_evidence": self.engine_config.trend_min_evidence,
            "time_insight_seasonal_min_years": self.engine_config.seasonal_min_years,
            "time_insight_ambient_max_insights": self.engine_config.static_max_insights,
            "time_insight_ambient_max_chars": self.engine_config.injection_max_chars,
            "time_insight_repeat_cooldown_minutes": self.repeat_cooldown_minutes,
            "time_insight_query_enable": self.query_scoped_enable,
            "time_insight_query_max_insights": self.engine_config.query_max_insights,
            "time_insight_query_min_score": self.engine_config.query_min_score,
            "time_insight_query_max_chars": self.engine_config.query_injection_max_chars,
            "time_insight_query_lookup_timeout": self.query_lookup_timeout,
            "time_insight_llm_refine_enable": self.llm_refine_enable,
            "time_insight_llm_provider_id": self.llm_provider_id,
            "time_insight_llm_timeout": self.llm_timeout,
            "time_insight_llm_min_confidence": self.llm_min_confidence,
            "time_insight_diagnostic_log": self.diagnostic_log,
        }

    async def status(self) -> dict[str, Any]:
        base = {
            "enabled": self.enable,
            "mode": "external_affiliate" if self.external_affiliate_active() else "integrated",
            "external_affiliate_active": self.external_affiliate_active(),
            "engine_version": _SCHEMA_VERSION,
            "settings": self.settings_snapshot(),
        }
        try:
            base.update(await asyncio.to_thread(self._status_sync))
        except FileNotFoundError as exc:
            base.update({"generated": False, "reason": str(exc)})
        except Exception as exc:
            base.update({"generated": False, "reason": str(exc)})
        return base

    async def update(self, reason: str = "manual", umo: str = "") -> dict[str, Any]:
        if not self.enable:
            return {"updated": False, "reason": "disabled"}
        if self.external_affiliate_active():
            return {"updated": False, "reason": "external_affiliate_active"}
        stats = await self._update(reason, umo)
        return {"updated": True, **stats}

    def schedule_refresh(self, reason: str = "memory_commit") -> bool:
        """Coalesce a post-commit refresh without delaying the memory writer."""
        if (
            self._stopping
            or not self.enable
            or self.auto_update_hours <= 0
            or self.external_affiliate_active()
        ):
            return False
        job_name = "memos-insight-post-commit"
        if any(not task.done() and task.get_name() == job_name for task in self._jobs):
            return False
        self._spawn(self._update(reason, self._last_umo), job_name)
        return True

    async def preview(self) -> dict[str, Any]:
        memories = await asyncio.to_thread(self._load_memories_sync)
        now = self._now()
        candidates, stats = await asyncio.to_thread(
            build_candidates, memories, now, self.engine_config
        )
        ambient = select_static_candidates(candidates, self.engine_config)
        block = render_static_block(ambient, now, self.engine_config)
        return {
            **stats,
            "ambient_selected": len(ambient),
            "ambient_block": block,
            "candidates": candidates[:16],
        }

    def _needs_update_sync(self) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT updated_ts, stats_json FROM memory_time_insights WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return True
        stats = _json_loads(row["stats_json"], {})
        if int(stats.get("engine_version") or 0) != _SCHEMA_VERSION:
            return True
        if str(stats.get("generated_date") or "") != self._now().date().isoformat():
            return True
        updated_ts = float(row["updated_ts"] or 0)
        return time.time() - updated_ts >= self.auto_update_hours * 3600

    @staticmethod
    def _aggregate_expr(
        columns: set[str], name: str, aggregate: str = "MAX", default: str = "''",
    ) -> str:
        if name in columns:
            return f"{aggregate}({name}) AS {name}"
        return f"{default} AS {name}"

    def _load_memories_sync(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            columns = self._columns(conn, "chunks")
            if not columns or "memo_name" not in columns:
                return []
            text_fields = (
                "ts_text", "tags", "source_session", "memory_type", "long_effect",
                "trigger_hint", "occurred_at", "time_basis", "scene_anchor",
                "retrieval_key", "state_change", "entities",
            )
            numeric_fields = (
                "importance", "manual", "created_ts", "event_ts",
                "source_created_ts", "source_updated_ts",
            )
            select_parts = ["memo_name"]
            select_parts.extend(
                self._aggregate_expr(columns, name) for name in text_fields
            )
            select_parts.extend(
                self._aggregate_expr(columns, name, "MAX", "0") for name in numeric_fields
            )
            select_parts.append(
                self._aggregate_expr(columns, "chunk_text", "MIN", "''")
            )
            sort_field = next(
                (name for name in ("event_ts", "source_created_ts", "created_ts") if name in columns),
                "",
            )
            order = f"ORDER BY MAX({sort_field}) DESC" if sort_field else "ORDER BY memo_name"
            sql = (
                "SELECT " + ", ".join(select_parts)
                + " FROM chunks GROUP BY memo_name "
                + order
                + " LIMIT " + chr(63)
            )
            rows = conn.execute(sql, (self.source_memo_limit,)).fetchall()
            feedback: dict[str, dict[str, Any]] = {}
            if self._table_exists(conn, "recall_feedback_events"):
                for row in conn.execute(
                    """SELECT memo_name, SUM(COALESCE(effect,0)) AS effect,
                              GROUP_CONCAT(DISTINCT action) AS actions
                       FROM recall_feedback_events
                       GROUP BY memo_name"""
                ).fetchall():
                    feedback[str(row["memo_name"])] = {
                        "feedback_effect": float(row["effect"] or 0),
                        "feedback_actions": row["actions"] or "",
                    }
            memories = []
            for row in rows:
                item = dict(row)
                item.update(feedback.get(str(item.get("memo_name") or ""), {}))
                memories.append(item)
            return memories
        finally:
            conn.close()
