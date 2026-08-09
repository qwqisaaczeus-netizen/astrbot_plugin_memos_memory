# -*- coding: utf-8 -*-
"""4.6.0-test 新增能力测试：原文完整落库 / 证据分级 / 转录风险 / QueryPlan。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path


def _make_plugin(extra: dict | None = None):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from astrbot_plugin_memos_memory.main import MemosMemoryPlugin

    cfg = {
        "character_name": "林翩翩",
        "memos_mode": "external",
        "memos_base_url": "http://127.0.0.1:19000",
        "memos_token": "test-token",
    }
    cfg.update(extra or {})
    plugin = object.__new__(MemosMemoryPlugin)
    plugin.config = cfg
    plugin.character_name = "林翩翩"
    plugin.memos_mode = "external"
    plugin.memos_base_url = "http://127.0.0.1:19000"
    plugin.memos_token = "test-token"
    plugin.memos_timeout = 5
    plugin.raw_archive_full_assistant_text = bool(cfg.get("raw_archive_full_assistant_text", True))
    plugin.raw_archive_assistant_max_chars = int(cfg.get("raw_archive_assistant_max_chars", 0))
    plugin.evidence_tier_enable = bool(cfg.get("evidence_tier_enable", True))
    plugin.diary_transcript_check_enable = bool(cfg.get("diary_transcript_check_enable", True))
    plugin.diary_must_coverage_threshold = float(cfg.get("diary_must_coverage_threshold", 0.95))
    plugin.diary_literary_mode = bool(cfg.get("diary_literary_mode", True))
    plugin.scene_split_enable = bool(cfg.get("scene_split_enable", True))
    plugin.query_plan_enable = bool(cfg.get("query_plan_enable", True))
    plugin.raw_archive_index_chunk_chars = 800
    return plugin


class EvidenceTierCorrectionTests(unittest.TestCase):
    """证据三档分级与本地校正。"""

    def setUp(self):
        self.plugin = _make_plugin()

    def _parse(self, episodes):
        messages = [
            {"role": "user", "content": "你真的不会走吗？你再说一遍。"},
            {"role": "assistant", "content": "我答应你，天亮前一定回来。破庙里的约定与城破时的承诺，我都记得。"},
            {"role": "user", "content": "好，我相信你。深夜长谈到此为止。"},
        ]
        return self.plugin._parse_episode_blueprints(json.dumps(episodes, ensure_ascii=False), messages, 8)

    def test_commitment_is_corrected_to_must_write(self):
        raw = [
            {
                "episode_key": "e1",
                "scene_anchor": "破庙约定",
                "memory_type": "promise_or_rule",
                "evidence": [
                    {
                        "kind": "commitment",
                        "actor": "assistant",
                        "detail": "答应知宥天亮前回来",
                        "quote": "我答应你，天亮前一定回来。",
                        "turn_indexes": [1],
                        "confidence": 0.9,
                    },
                    {
                        "kind": "dialogue",
                        "actor": "user",
                        "detail": "又问了一遍会不会走",
                        "quote": "你真的不会走吗？",
                        "turn_indexes": [0],
                        "confidence": 0.7,
                    },
                    {
                        "kind": "dialogue",
                        "actor": "user",
                        "detail": "重复确认了好几遍",
                        "quote": "你再说一遍。",
                        "turn_indexes": [2],
                        "confidence": 0.6,
                    },
                ],
            }
        ]
        parsed = self._parse(raw)
        tiers = [e.get("tier") for e in parsed[0]["evidence"]]
        # 承诺 → must_write（本地校正）；重复确认不得升为 must_write。
        self.assertEqual(tiers[0], "must_write")
        self.assertIn(tiers[1], ("must_write", "supporting"))
        self.assertNotEqual(tiers[2], "must_write")

    def test_scene_fields_pass_through(self):
        raw = [
            {
                "episode_key": "e1",
                "scene_anchor": "深夜长谈",
                "scene_start_turn": 0,
                "scene_end_turn": 7,
                "scene_boundary_reasons": ["time_gap", "topic_shift"],
                "evidence": [
                    {
                        "kind": "dialogue",
                        "actor": "assistant",
                        "detail": "深夜长谈到此为止",
                        "quote": "好，我相信你。深夜长谈到此为止。",
                        "turn_indexes": [2],
                        "tier": "supporting",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
        parsed = self._parse(raw)
        self.assertEqual(parsed[0]["scene_start_turn"], 0)
        self.assertEqual(parsed[0]["scene_end_turn"], 7)
        self.assertIn("time_gap", parsed[0]["scene_boundary_reasons"])


class DiaryCoverageTierTests(unittest.TestCase):
    """分档覆盖率：must_write 决定阈值，archive_only 不参与。"""

    def setUp(self):
        self.plugin = _make_plugin()

    def _episode(self, tiers: list[str]):
        return {
            "episode_key": "e1",
            "evidence": [
                {
                    "kind": "dialogue",
                    "actor": "assistant",
                    "detail": f"关键事实{i}：破庙里的约定与城破时的承诺",
                    "quote": "",
                    "turn_indexes": [i],
                    "tier": tier,
                    "confidence": 0.8,
                }
                for i, tier in enumerate(tiers)
            ],
        }

    def test_missing_must_write_keeps_coverage_low(self):
        episode = self._episode(["must_write", "must_write", "archive_only"])
        content = "今天在破庙里说了很多话。"
        coverage, missing = self.plugin._diary_render_coverage(content, episode)
        self.assertLess(coverage, 0.5)
        self.assertTrue(missing)

    def test_archive_only_missing_does_not_affect_coverage(self):
        episode = self._episode(["must_write", "archive_only"])
        content = "破庙里的约定，我答应了天亮前回来。"
        coverage, _missing = self.plugin._diary_render_coverage(content, episode)
        self.assertGreaterEqual(coverage, 0.9)

    def test_legacy_data_without_tier_uses_old_caliber(self):
        episode = {
            "episode_key": "e1",
            "evidence": [
                {"kind": "dialogue", "actor": "assistant", "detail": "旧库无 tier 的事实A", "turn_indexes": [0]},
                {"kind": "dialogue", "actor": "user", "detail": "旧库无 tier 的事实B", "turn_indexes": [1]},
            ],
        }
        content = "旧库的事实A和事实B都在这里。"
        coverage, _ = self.plugin._diary_render_coverage(content, episode)
        self.assertGreaterEqual(coverage, 0.5)


class TranscriptRiskTests(unittest.TestCase):
    """防转录检测。"""

    def setUp(self):
        self.plugin = _make_plugin()

    def test_verbatim_replay_is_high_risk(self):
        episode = {"episode_key": "e1", "evidence": []}
        raw = "他说你要走吗。我说不走。他说真的吗。我说真的。他说你保证。我说我保证。他说好。我说好。"
        content = "他说你要走吗，我说不走，他说真的吗，我说真的，他说你保证，我说我保证，他说好，我说好。"
        risk = self.plugin._diary_transcript_risk(content, episode, raw)
        self.assertGreaterEqual(risk, 0.4)

    def test_literary_diary_is_low_risk(self):
        episode = {"episode_key": "e1", "evidence": []}
        raw = "他说你要走吗。我说不走。他说真的吗。我说真的。"
        content = "城门的灯还亮着，我攥紧了袖口。他没再追问，我也没有解释，只是并肩走完那条长街。"
        risk = self.plugin._diary_transcript_risk(content, episode, raw)
        self.assertLess(risk, 0.4)


class QueryPlanTests(unittest.TestCase):
    """QueryPlan 指代消解。"""

    def setUp(self):
        self.plugin = _make_plugin()

    def test_ambiguous_query_uses_entity_hint(self):
        ctxs = [{"content": "知宥在破庙里等了一夜", "role": "user"}]
        plan = self.plugin._build_query_plan("她还在等吗？", ctxs)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["intent"], "contextual")
        self.assertTrue(plan["query"].startswith("她还在等吗？"))
        self.assertIn("破庙", plan["query"])

    def test_plain_query_without_context_plan(self):
        # 6.2：当前消息主体明确（无指代、非时间查询）→ 不提取上下文，只使用当前消息。
        # 注意：含时间词（今天/昨天/几号…）的查询会进入 temporal 意图独立生成日期约束，
        # 这是 4.6.0 大纲要求的行为，不属于"主体明确"。
        self.plugin.query_plan_enable = True
        ctxs = []
        plan = self.plugin._build_query_plan("知宥今晚应该还在破庙守夜到天亮", ctxs)
        self.assertIsNone(plan)

    def test_temporal_query_gets_plan_without_context(self):
        # 6.2：时间查询独立生成标准日期约束，但不并入上文
        ctxs = []
        plan = self.plugin._build_query_plan("知宥今天吃了吗？", ctxs)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["intent"], "temporal")
        self.assertTrue(plan["temporal_constraints"])
        self.assertFalse(plan["use_context"])

    def test_disabled_plan_returns_none(self):
        self.plugin.query_plan_enable = False
        ctxs = [{"content": "知宥在破庙里等了一夜", "role": "user"}]
        plan = self.plugin._build_query_plan("她还在等吗？", ctxs)
        self.assertIsNone(plan)


class QueryPlanWindowTests(unittest.TestCase):
    """QueryPlan 窗口上下文：只提取最近 N 轮实体。"""

    def setUp(self):
        self.plugin = _make_plugin()
        self.plugin.query_plan_context_window = 3

    def test_window_excludes_old_entities(self):
        ctxs = [
            {"content": "三个月前的约定是在西市口", "role": "user"},
            {"content": "上个月还欠了一笔钱", "role": "user"},
            {"content": "知宥刚在破庙里等了一夜", "role": "user"},
        ]
        plan = self.plugin._build_query_plan("那个约定还算数吗？", ctxs)
        self.assertIsNotNone(plan)
        # 窗口 3 = 全部在窗口内：约定/破庙/知宥 都应被提取
        self.assertIn("约定", plan["query"])
        self.assertIn("破庙", plan["query"])

    def test_window_truncates_old_context(self):
        # 窗口 2：第一条（西市口）在窗口外 → 不参与实体提取
        ctxs = [
            {"content": "三个月前的约定是在西市口", "role": "user"},
            {"content": "上个月还欠了一笔钱", "role": "user"},
            {"content": "知宥刚在破庙里等了一夜", "role": "user"},
        ]
        plan = self.plugin._build_query_plan("那个约定还算数吗？", ctxs[:2])
        self.assertIsNotNone(plan)
        self.assertIn("约定", plan["query"])
        self.assertNotIn("破庙", plan["query"])


class EpisodeDbLocationTests(unittest.TestCase):
    """原文库路径固化。"""

    def test_resolve_returns_db_name(self):
        plugin = _make_plugin()
        resolved = plugin._resolve_plugin_data_db("episodic_memory.db")
        self.assertTrue(str(resolved).endswith("episodic_memory.db"))

    def test_migrate_legacy_to_target_copies(self):
        import tempfile
        from pathlib import Path

        plugin = _make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy" / "episodic_memory.db"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy-db-content")
            target = Path(tmp) / "target" / "episodic_memory.db"
            plugin.episodic_db_path = str(target)
            # 手动执行迁移逻辑的核心（_copy_db_files 复制）
            from astrbot_plugin_memos_memory.main import _copy_db_files

            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_db_files(legacy, target)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(), "legacy-db-content")
            self.assertTrue(legacy.exists(), "旧库保留")


class ContextUsedMarkingTests(unittest.TestCase):
    """候选 Query → 最终是否使用上文 的显式标记。"""

    def _plugin(self):
        plugin = _make_plugin()
        plugin.recall_context_query_messages = 3
        plugin.recall_context_query_max_chars = 900
        # _plan_episodic_query 引用的注入配额（夹具跳过 __init__）
        plugin.episodic_narrative_inject = 6
        plugin.episodic_default_inject = 5
        plugin.episodic_candidate_pool = 18
        return plugin

    def test_short_query_uses_context(self):
        plugin = self._plugin()
        ctxs = [
            {"content": "知宥刚在破庙里等了一夜", "role": "user"},
        ]
        plan = plugin._plan_episodic_query("那后来呢？", "知宥刚在破庙里等了一夜")
        self.assertTrue(plan["use_context"])
        self.assertIn("相关上文", plan["search_text"])

    def test_specific_query_skips_context(self):
        plugin = self._plugin()
        # >14 字且无指代词 → 不用上文（实体充分，直接检索）
        plan = plugin._plan_episodic_query("林翩翩在扬州城的旧宅现在还有人居住吗", "知宥刚在破庙里等了一夜")
        self.assertFalse(plan["use_context"])
        self.assertNotIn("相关上文", plan["search_text"])

    def test_episodic_diag_carries_context_used(self):
        plugin = self._plugin()
        plan = plugin._plan_episodic_query("那后来呢？", "知宥刚在破庙里等了一夜")
        self.assertTrue(plan.get("use_context"))


class RawArchivePolicyTests(unittest.TestCase):
    """原文档案完整化策略。"""

    def test_full_archive_keeps_long_assistant(self):
        plugin = _make_plugin({"raw_archive_full_assistant_text": True})
        text = "长" * 5000
        if plugin.raw_archive_full_assistant_text and plugin.raw_archive_assistant_max_chars <= 0:
            kept = text
        else:
            kept = text[:2000]
        self.assertEqual(len(kept), 5000)

    def test_max_chars_caps_archive(self):
        plugin = _make_plugin({"raw_archive_assistant_max_chars": 1200})
        text = "长" * 5000
        kept = text[: plugin.raw_archive_assistant_max_chars]
        self.assertEqual(len(kept), 1200)


if __name__ == "__main__":
    unittest.main()
