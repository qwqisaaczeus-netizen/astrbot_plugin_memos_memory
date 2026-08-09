import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot_plugin_memos_memory.main import (
    MemosMemoryPlugin,
    _DEFAULT_IMP_TIER4,
    _LEGACY_IMP_TIER4,
    _migrate_default_keywords,
)
from astrbot_plugin_memos_memory.time_insight_engine import (
    EngineConfig,
    build_candidates,
    render_query_block,
    select_query_candidates,
    select_static_candidates,
)
from astrbot_plugin_memos_memory.time_insight_service import IntegratedTimeInsightService


def memory(name, occurred_at, memory_type="relationship_shift", importance=4, text="关系更信任"):
    return {
        "memo_name": name,
        "occurred_at": occurred_at,
        "ts_text": occurred_at,
        "time_basis": "explicit",
        "importance": importance,
        "manual": 0,
        "memory_type": memory_type,
        "state_change": text,
        "long_effect": text,
        "trigger_hint": "再次谈到约定时自然想起",
        "scene_anchor": "雨夜约定",
        "chunk_text": text,
        "entities": '["爱莉"]',
        "tags": '["约定"]',
    }


class TierKeywordTests(unittest.TestCase):
    def test_legacy_default_is_upgraded_but_custom_value_is_preserved(self):
        self.assertEqual(
            _migrate_default_keywords(_LEGACY_IMP_TIER4, _LEGACY_IMP_TIER4, _DEFAULT_IMP_TIER4),
            _DEFAULT_IMP_TIER4,
        )
        self.assertEqual(
            _migrate_default_keywords("我的专属高权重词", _LEGACY_IMP_TIER4, _DEFAULT_IMP_TIER4),
            "我的专属高权重词",
        )

    def test_expanded_tier_words_still_drive_fallback_importance(self):
        plugin = MemosMemoryPlugin.__new__(MemosMemoryPlugin)
        plugin.imp_tier5_keywords = "关系定义改变"
        plugin.imp_tier4_keywords = "建立信任,坦白脆弱"
        plugin.imp_tier3_keywords = "共同习惯"
        plugin.imp_low_keywords = "普通聊天"
        self.assertEqual(plugin._auto_importance("那天起，我们的关系定义改变了。"), 5)
        self.assertEqual(plugin._auto_importance("她终于建立信任，也愿意坦白脆弱。"), 4)
        self.assertEqual(plugin._auto_importance("睡前问好成了共同习惯。"), 3)


class TimeInsightEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 3, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.cfg = EngineConfig(
            timezone="Asia/Shanghai",
            recent_window_days=21,
            min_evidence_score=0.62,
            trend_min_distinct_days=3,
            trend_min_evidence=3,
            seasonal_min_years=3,
            static_max_insights=1,
        )

    def test_candidates_require_independent_dates_and_keep_query_only_patterns_out_of_ambient(self):
        memories = [memory("memos/anniversary", "2025-08-03", text="在雨夜正式确认彼此的关系")]
        memories.extend([
            memory("memos/trend-1", "2026-07-20", text="开始更愿意坦白自己的担心"),
            memory("memos/trend-2", "2026-07-25", text="冲突后仍愿意回来沟通"),
            memory("memos/trend-3", "2026-08-01", text="面对不安时选择直接说明"),
        ])
        memories.extend([
            memory("memos/season-2023", "2023-08-10", text="八月更容易想起共同生活"),
            memory("memos/season-2024", "2024-08-11", text="八月谈到共同生活的计划"),
            memory("memos/season-2025", "2025-08-12", text="八月再次讨论共同生活"),
        ])
        candidates, stats = build_candidates(memories, self.now, self.cfg)
        self.assertGreaterEqual(stats["candidate_count"], 2)
        kinds = {item["kind"] for item in candidates}
        self.assertIn("anniversary_exact", kinds)
        self.assertIn("recent_trend", kinds)
        ambient = select_static_candidates(candidates, self.cfg)
        self.assertLessEqual(len(ambient), 1)
        self.assertTrue(all(item["kind"] in {"anniversary_exact", "recent_trend"} for item in ambient))
        self.assertNotIn("seasonal_pattern", {item["kind"] for item in ambient})

    def test_query_selection_is_evidence_bound(self):
        candidates, _ = build_candidates(
            [memory("memos/anniversary", "2025-08-03", text="和爱莉在雨夜确认约定")],
            self.now,
            self.cfg,
        )
        select_static_candidates(candidates, self.cfg)
        selected = select_query_candidates("爱莉，还记得去年今天的雨夜约定吗", candidates, self.cfg)
        self.assertEqual(len(selected), 1)
        block = render_query_block(selected, self.now, self.cfg)
        self.assertIn("不是当前发生的事件", block)
        self.assertIn("2025-08-03", block)


class FakeContext:
    def __init__(self, external=False):
        self.external = external

    def get_registered_star(self, name):
        if self.external and name == "astrbot_plugin_memos_memory_insight":
            return SimpleNamespace(activated=True)
        return None

    def get_using_provider(self, _umo=""):
        return None


class FakeHost:
    def __init__(self, db_path, external=False, episodic_db_path=None):
        self.context = FakeContext(external=external)
        self.vec_db_path = str(db_path)
        self.episodic_db_path = str(episodic_db_path or Path(db_path).with_name("episodic_memory.db"))
        self.rp_time_timezone = "Asia/Shanghai"
        self.config = {
            "enable_time_insight_affiliate": True,
            "time_insight_auto_update_hours": 0,
            "time_insight_llm_refine_enable": False,
            "time_insight_min_evidence_score": 0.60,
            "time_insight_ambient_max_insights": 1,
            "time_insight_repeat_cooldown_minutes": 180,
        }

    def _semantic_state_status(self):
        return {
            "state": {
                "rendered_text": "关系位置：愿意直接沟通；当前边界：不把历史日期当作正在发生。",
            },
        }


class FakeEvent:
    def __init__(self, text):
        self.message_str = text
        self.unified_msg_origin = "test:session"
        self.extra = {}

    def set_extra(self, key, value):
        self.extra[key] = value


class TimeInsightServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def create_db(path):
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE chunks (
                memo_name TEXT, chunk_text TEXT, ts_text TEXT, tags TEXT,
                source_session TEXT, memory_type TEXT, long_effect TEXT,
                trigger_hint TEXT, occurred_at TEXT, time_basis TEXT,
                scene_anchor TEXT, retrieval_key TEXT, state_change TEXT,
                entities TEXT, importance INTEGER, manual INTEGER,
                created_ts REAL, event_ts REAL, source_created_ts REAL,
                source_updated_ts REAL
            )"""
        )
        today = datetime.now(ZoneInfo("Asia/Shanghai"))
        occurred = f"{today.year - 1:04d}-{today.month:02d}-{today.day:02d}"
        conn.execute(
            """INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "memos/anniversary", "和爱莉在雨夜确认了重要约定", occurred,
                '["约定"]', "test", "promise_or_rule", "会认真对待这份约定",
                "谈到雨夜时想起", occurred, "explicit", "雨夜约定", "爱莉 雨夜 约定",
                "关系更信任", '["爱莉"]', 5, 0, time.time(), 0, time.time(), time.time(),
            ),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def create_episode_db(path):
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY, memo_name TEXT, source_kind TEXT,
                evidence_quality TEXT, occurred_at TEXT, event_ts REAL,
                time_basis TEXT, memory_type TEXT, importance INTEGER,
                scene_anchor TEXT, retrieval_key TEXT, state_change TEXT,
                long_effect TEXT, trigger_hint TEXT, entities_json TEXT,
                active INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE episode_turn_links (
                episode_id TEXT, batch_id TEXT, turn_index INTEGER, evidence_index INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE episode_evidence (
                episode_id TEXT, grounded INTEGER
            )"""
        )
        today = datetime.now(ZoneInfo("Asia/Shanghai"))
        occurred = f"{today.year - 1:04d}-{today.month:02d}-{today.day:02d}"
        conn.execute(
            "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ep-anniversary", "memos/anniversary", "episode_extraction",
                "source_grounded", occurred, 0, "explicit", "promise_or_rule", 5,
                "雨夜约定", "爱莉 雨夜 约定", "关系更信任", "会认真对待约定",
                "谈到雨夜时自然想起", '["爱莉"]', 1,
            ),
        )
        conn.execute(
            "INSERT INTO episode_turn_links VALUES (?,?,?,?)",
            ("ep-anniversary", "batch-1", 2, 0),
        )
        conn.execute(
            "INSERT INTO episode_evidence VALUES (?,?)",
            ("ep-anniversary", 1),
        )
        conn.commit()
        conn.close()

    async def test_integrated_update_and_request_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "memories.db"
            self.create_db(db)
            service = IntegratedTimeInsightService(FakeHost(db))
            result = await service.update("test")
            self.assertTrue(result["updated"])
            self.assertGreaterEqual(result["candidate_count"], 1)
            request = SimpleNamespace(extra_user_content_parts=[])
            event = FakeEvent("爱莉，还记得去年今天的雨夜约定吗")
            injected = await service.on_request(event, request)
            self.assertGreater(injected["chars"], 0)
            self.assertEqual(injected["selected"], 1)
            self.assertIn("memos_time_insight_v3", event.extra)
            self.assertIn("不是当前事件", request.extra_user_content_parts[-1].text)

    async def test_v4_uses_episode_traceability_and_one_request_time_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "memories.db"
            episode_db = root / "episodic_memory.db"
            self.create_db(db)
            self.create_episode_db(episode_db)
            service = IntegratedTimeInsightService(FakeHost(db, episodic_db_path=episode_db))
            result = await service.update("v4-grounding")
            self.assertEqual(result["engine_version"], 4)
            self.assertEqual(result["source_grounded_memories"], 1)
            self.assertEqual(result["source_recoverable_memories"], 1)
            request = SimpleNamespace(extra_user_content_parts=[])
            event = FakeEvent("爱莉，还记得去年今天的雨夜约定吗")
            snapshot = datetime(2026, 8, 9, 3, 4, 5, tzinfo=ZoneInfo("UTC"))
            injected = await service.on_request(event, request, request_now=snapshot)
            self.assertEqual(injected["source_recoverable"], 1)
            self.assertIn("memos/anniversary", injected["evidence_memos"])
            self.assertIn("2026-08-09 11:04:05", request.extra_user_content_parts[-1].text)
            self.assertIn("memos_time_insight_v4", event.extra)

    async def test_external_affiliate_yields_without_double_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "memories.db"
            self.create_db(db)
            service = IntegratedTimeInsightService(FakeHost(db, external=True))
            request = SimpleNamespace(extra_user_content_parts=[])
            result = await service.on_request(FakeEvent("还记得那天吗"), request)
            self.assertEqual(result["mode"], "external_affiliate")
            self.assertEqual(request.extra_user_content_parts, [])

    def test_ambient_repeat_cooldown_does_not_block_explicit_temporal_query(self):
        with tempfile.TemporaryDirectory() as temp:
            service = IntegratedTimeInsightService(FakeHost(Path(temp) / "memories.db"))
            candidate = [{"candidate_id": "anniversary-1"}]
            self.assertTrue(service._allow_ambient("scope", candidate, "你好"))
            self.assertFalse(service._allow_ambient("scope", candidate, "继续说"))
            self.assertTrue(service._allow_ambient("scope", candidate, "还记得去年今天吗"))

    async def test_post_commit_refresh_is_coalesced_and_respects_auto_update(self):
        with tempfile.TemporaryDirectory() as temp:
            host = FakeHost(Path(temp) / "memories.db")
            host.config["time_insight_auto_update_hours"] = 12
            service = IntegratedTimeInsightService(host)
            release = asyncio.Event()
            calls = []

            async def update(reason, umo=""):
                calls.append((reason, umo))
                await release.wait()
                return {}

            service._update = update
            self.assertTrue(service.schedule_refresh("eod_checkpoint"))
            self.assertFalse(service.schedule_refresh("eod_checkpoint"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*list(service._jobs))
            self.assertEqual(calls, [("eod_checkpoint", "")])
            await service.terminate()


if __name__ == "__main__":
    unittest.main()
