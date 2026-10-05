# -*- coding: utf-8 -*-
"""全随机全功能测试（独立轮次，种子由环境变量 RAND_SEED 控制）。

每轮：随机角色/地点/事件/物品/消息/篇目/场景区间/证据（tier/quote/grounded/confidence）/
参数（窗口/线索/阈值/间隔/分块），跑核心本地链路 + 原文库累积一致性，逐批 8 项检查。

用法：
  RAND_SEED=20260807_1 python -m unittest tests.test_randomized_full -v
至少三轮独立运行 = 换种子跑三次（每次独立进程）。
"""

import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from astrbot_plugin_memos_memory.compress import build_episode_extraction_prompt, format_messages_for_prompt
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore

# 随机池（全随机数据源）
NAMES = ["知宥", "林晚", "K-9", "白芷", "沈听澜", "洛央", "顾星辞", "苏晚棠", "阿蛮", "江辞", "云深", "崔九"]
LOCATIONS = ["破庙", "地铁口", "殖民船", "长安街", "竹林", "实验室", "海边", "雪山营地", "夜市", "旧书店"]
EVENTS = ["守夜", "裁员通知", "探测故障", "重逢", "吵架", "告白", "承诺约定", "告别",
          "寻找失物", "生病照顾", "结盟", "背叛", "失约", "生日惊喜"]
ITEMS = ["寒霜刀", "玉簪", "数据卡", "怀表", "书信", "药包", "指环", "地图"]
KINDS = ["dialogue", "action", "fact", "object", "commitment", "boundary", "body", "setting"]
TIERS = ["must_write", "supporting", "archive_only"]
REASONS = ["time_gap", "date_change", "topic_shift", "relation_turn", "location_change", "arc_end"]


def make_plugin(rng: random.Random, extra: dict | None = None):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from astrbot_plugin_memos_memory.main import MemosMemoryPlugin

    cfg = {
        "character_name": rng.choice(NAMES),
        "memos_mode": "external",
        "memos_base_url": "http://127.0.0.1:19000",
        "memos_token": "test-token",
    }
    cfg.update(extra or {})
    plugin = object.__new__(MemosMemoryPlugin)
    plugin.config = cfg
    plugin.character_name = cfg["character_name"]
    plugin.memos_mode = "external"
    plugin.memos_base_url = "http://127.0.0.1:19000"
    plugin.memos_token = "test-token"
    plugin.memos_timeout = 5
    plugin.rp_time_timezone = "Asia/Shanghai"
    plugin.raw_archive_full_assistant_text = True
    plugin.raw_archive_assistant_max_chars = 0
    plugin.raw_archive_index_chunk_chars = rng.randint(200, 4000)
    plugin.raw_archive_prompt_view_max_chars = rng.choice([0, 400, 800, 1200, 2000])
    plugin.scene_split_enable = rng.choice([True, True, False])
    plugin.scene_split_gap_seconds = float(rng.randint(60, 86400))
    plugin.diary_count = rng.randint(1, 3)
    plugin.diary_count_max_cap = rng.randint(3, 8)
    plugin.evidence_tier_enable = True
    plugin.diary_literary_mode = True
    plugin.diary_transcript_check_enable = rng.choice([True, True, False])
    plugin.diary_must_coverage_threshold = round(rng.uniform(0.3, 0.95), 2)
    plugin.diary_first_person_check_enable = True
    plugin.query_plan_enable = True
    plugin.query_plan_context_window = rng.randint(1, 15)
    plugin.query_plan_llm_disambiguate = False
    plugin.query_plan_llm_confidence_threshold = 0.5
    plugin.query_plan_llm_provider_id = ""
    plugin.diary_rewrite_preview_min_chars = rng.randint(800, 4000)
    plugin.db_snapshot_keep = rng.randint(1, 5)
    plugin.evidence_first_generation_enable = True
    plugin.episode_extraction_provider_id = ""
    plugin.diary_render_provider_id = ""
    plugin.episode_extraction_timeout = 60
    plugin.diary_render_timeout = 60
    plugin.eod_checkpoint_min_turns = 1
    # _plan_episodic_query 依赖
    plugin.episodic_narrative_inject = rng.randint(2, 6)
    plugin.episodic_default_inject = rng.randint(2, 5)
    plugin.episodic_candidate_pool = rng.randint(12, 40)
    return plugin


def random_messages(rng: random.Random, batch_index: int) -> list[dict]:
    """随机 4-8 轮消息；事件时间在 1-3 天内随机分布（含同日期/跨午夜/大间隔）。"""
    n_turns = rng.randint(4, 8)
    day = 1 + batch_index % 3
    base_ts = datetime(2026, 8, day, rng.randint(8, 22), rng.randint(0, 59), tzinfo=timezone.utc).timestamp()
    messages = []
    ts = base_ts
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        person = rng.choice(NAMES)
        location = rng.choice(LOCATIONS)
        event = rng.choice(EVENTS)
        item = rng.choice(ITEMS)
        content = f"{person}在{location}{event}时提到了{item}，这是第{i}条随机消息"
        if rng.random() < 0.15:
            content += "，重复确认了一遍又一遍"
        if rng.random() < 0.1:
            ts += rng.uniform(3 * 3600, 30 * 3600)  # 大间隔
        else:
            ts += rng.uniform(60, 3600)
        messages.append({"role": role, "content": content, "event_ts": ts, "event_timezone": "Asia/Shanghai"})
    return messages


def random_episodes(rng: random.Random, messages: list[dict]) -> list[dict]:
    """随机 1-3 个篇目：随机场景区间 + 2-6 条随机证据（tier/quote/grounded/confidence）。"""
    total = len(messages)
    n_episodes = rng.randint(1, 3)
    boundaries = sorted(rng.sample(range(1, total), min(rng.randint(0, 2), max(0, total - 1)))) if total > 1 else []
    starts = [0] + boundaries
    ends = [b - 1 for b in boundaries] + [total - 1]
    episodes = []
    for e in range(min(n_episodes, len(starts))):
        start = starts[e]
        end = ends[e]
        if end < start:
            start, end = 0, total - 1
        evidence = []
        for _ in range(rng.randint(2, 6)):
            idx = rng.randint(start, end)
            quote = messages[idx]["content"]
            if rng.random() < 0.3:
                quote = "不在消息里的原话" + rng.choice(["嗯", "啊", "好"])  # 不可追溯引语
            kind = rng.choice(KINDS)
            tier = rng.choice(TIERS)
            if kind in ("commitment", "boundary"):
                tier = rng.choice(["supporting", "archive_only", "must_write"])  # 校正前可能标低
            evidence.append({
                "kind": kind,
                "actor": rng.choice(["user", "assistant", "双方"]),
                "detail": f"{rng.choice(NAMES)}{rng.choice(EVENTS)}的关键细节{_}", 
                "quote": quote,
                "turn_indexes": [idx],
                "tier": tier,
                "confidence": round(rng.uniform(0.2, 0.95), 2),
            })
        episodes.append({
            "episode_key": f"e{e + 1}",
            "event_date": "2026-08-01",
            "time_label": rng.choice(["上午", "下午", "晚上"]),
            "time_basis": "conversation_now",
            "scene_anchor": rng.choice(LOCATIONS) + rng.choice(EVENTS),
            "scene_start_turn": start,
            "scene_end_turn": end,
            "scene_boundary_reasons": rng.sample(REASONS, rng.randint(0, 3)),
            "memory_type": "plot_fact",
            "evidence": evidence,
            "affect_before": "平静",
            "affect_after": rng.choice(["安心", "失落", "坚定"]),
            "state_change": "关系更近一步",
            "long_effect": "以后会更认真对待",
            "trigger_hint": "再次谈到" + rng.choice(EVENTS) + "时",
            "retrieval_key": rng.choice(NAMES) + rng.choice(LOCATIONS) + rng.choice(EVENTS),
            "entities": [rng.choice(NAMES), rng.choice(LOCATIONS)],
            "unresolved": [],
            "tags": [],
            "importance": rng.randint(1, 5),
        })
    return episodes


class RandomizedFullSuite(unittest.TestCase):
    """独立轮次：RAND_SEED 控制；每轮随机数据 + 随机参数，逐批 8 项检查 + 库一致性。"""

    SEED = os.environ.get("RAND_SEED", "20260807_1")

    @classmethod
    def setUpClass(cls):
        cls.rng = random.Random(cls.SEED)
        cls.plugin = make_plugin(cls.rng)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = EpisodicStore(str(Path(cls.tmp.name) / "episodic.db"), 3, "random-test-emb")
        asyncio_loop = None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.close()
        except Exception:
            pass
        try:
            cls.tmp.cleanup()
        except Exception:
            pass

    def _check_store_consistency(self, batch_label: str):
        """检查 8：库累积一致性——stats/traceability 与真实行数对账。"""
        stats = self.store.stats()
        trace = self.store.traceability()
        self.assertGreaterEqual(int(stats["source_turns"]), 0)
        self.assertEqual(int(stats["source_batches"]), int(trace["batches"]))
        self.assertLessEqual(int(trace["episodes_with_source_batch"]), int(trace["episodes_total"]))
        self.assertLessEqual(int(trace["exact_turn_link_count"]), int(trace["episodes_total"]) * 8 + 1)
        if int(stats["source_turns"]) > 0:
            self.assertTrue(trace["source_lexical_ready"], "有轮次就应有词面索引")
        hits = self.store.search_source_turns([0.1] * 3, "随机消息", limit=8)
        self.assertIsInstance(hits, list)
        cards = self.store.search_cards([0.1] * 3, "随机", limit=8)
        self.assertIsInstance(cards, list)

    def test_randomized_batches(self):
        n_batches = 30
        for batch_index in range(n_batches):
            with self.subTest(batch=batch_index, seed=self.SEED):
                messages = random_messages(self.rng, batch_index)
                episodes = random_episodes(self.rng, messages)
                # 模拟 LLM 输出
                llm_text = json.dumps(episodes, ensure_ascii=False)
                # 检查 1：蓝图解析不崩、tier 合法
                parsed = self.plugin._parse_episode_blueprints(llm_text, messages, max(1, len(episodes) + 1))
                self.assertIsInstance(parsed, list)
                for ep in parsed:
                    for ev in ep.get("evidence") or []:
                        self.assertIn(ev["tier"], TIERS)
                    self.assertLess(int(ep["scene_start_turn"]), len(messages))
                # 检查 2：commitment 校正为 must_write
                for ep in parsed:
                    for ev in ep.get("evidence") or []:
                        if ev["kind"] in ("commitment", "boundary"):
                            self.assertEqual(ev["tier"], "must_write", "commitment 必须本地校正为 must_write")
                # 检查 3：不可追溯引语被清空
                for ep in parsed:
                    for ev in ep.get("evidence") or []:
                        self.assertNotIn("不在消息里的原话", ev["quote"])
                # 检查 4：场景候选 + 区间校验（越界压入/重叠/未覆盖均不崩）
                candidates = self.plugin._detect_scene_candidates(messages)
                report = self.plugin._validate_episode_ranges(parsed, messages, candidates or None)
                self.assertIn("fixed", report)
                self.assertIn("overlaps", report)
                self.assertIn("uncovered", report)
                # 检查 5：QueryPlan 全字段
                ctxs = [{"role": m["role"], "content": m["content"], "event_ts": m["event_ts"]} for m in messages[-4:]]
                query = rng_pick_query(self.rng, messages)
                plan = self.plugin._build_query_plan(query, ctxs)
                if plan is not None:
                    for key in ("standalone_query", "resolved_entities", "relation_cues", "emotion_cues",
                                "temporal_constraints", "intent", "context_turn_indexes", "confidence", "use_context"):
                        self.assertIn(key, plan)
                # 检查 6：_plan_episodic_query reason 一致性
                ctx_text = "、".join((plan or {}).get("resolved_entities") or [])
                plan_ctx = self.plugin._plan_episodic_query(query, ctx_text)
                self.assertIn(plan_ctx["context_used_reason"],
                              {"no_candidate", "ambiguous", "broad_sparse", "specific_query",
                               "temporal_query", "narrative_entities"})
                self.assertEqual(plan_ctx["use_context"],
                                 bool(ctx_text and plan_ctx["context_used_reason"] in
                                      ("ambiguous", "broad_sparse", "narrative_entities")))
                # 检查 7：转录风险分档（照抄 ≥ 文学改写）+ 第一人称校验
                raw = "\n".join(str(m["content"]) for m in messages)
                verbatim = raw[:300]  # test2 使用滚动哈希 LCS；这里仍控制随机套件单批输入规模
                literary = "我把那条路走完了，风很冷，可是谁都没有开口说再见。"
                risk_verbatim = self.plugin._diary_transcript_risk(verbatim, {"evidence": []}, raw)
                risk_literary = self.plugin._diary_transcript_risk(literary, {"evidence": []}, raw)
                self.assertGreaterEqual(risk_verbatim, risk_literary)
                ok, _ = self.plugin._diary_first_person_check(literary, {"evidence": []})
                self.assertTrue(ok)
                bad, reason = self.plugin._diary_first_person_check("用户说：我们走吧。", {"evidence": []})
                self.assertFalse(bad)
                self.assertEqual(reason, "platform_role_subject")
                # 库累积（检查 8 在最后统一对账）
                batch_id = self.store.archive_batch(f"rand-session-{batch_index}", messages, "auto")
                self.assertTrue(batch_id)
                self.assertEqual(len(self.store.source_turns(batch_id)), len([m for m in messages if m.get("content")]))
                for ep in parsed:
                    self.store.upsert_episode(
                        memo_name=f"memos/rand-{batch_index}-{ep['episode_key']}",
                        episode=ep,
                        card_text=ep["scene_anchor"] + ep["retrieval_key"],
                        embedding=[0.5, 0.3, 0.1],
                        source_batch_id=batch_id,
                        source_kind="random",
                        legacy=False,
                        evidence_quality="source_grounded",
                        diary_content_hash=f"hash-{batch_index}-{ep['episode_key']}",
                        must_coverage=round(self.rng.uniform(0.8, 1.0), 3),
                        support_coverage=round(self.rng.uniform(0.5, 1.0), 3),
                        transcript_risk=round(self.rng.uniform(0.0, 0.4), 3),
                        diary_render_version="4.6.0-random",
                    )
        self._check_store_consistency("final")

    def test_view_separation_random(self):
        """视图分离：随机长轮次在视图中截断并带映射标记，原文完整。"""
        rng = self.rng
        long_content = "甲" * rng.randint(2000, 5000)
        contexts = [{"role": "user", "content": long_content, "event_ts": 1785762600.0}]
        cap = rng.choice([400, 800, 1200])
        view = format_messages_for_prompt(contexts, timezone_name="Asia/Shanghai", max_chars_per_turn=cap)
        self.assertNotIn("甲" * (cap + 100), view)
        self.assertIn("省略", view)
        prompt = build_episode_extraction_prompt(
            "测试角色", "[turn:0] 用户: hi", 2,
            scene_candidates=[{"start_turn": 0, "end_turn": 3, "reasons": ["time_gap"]}],
            diary_cap=6,
        )
        self.assertIn("候选区间", prompt)


def rng_pick_query(rng: random.Random, messages: list[dict]) -> str:
    """随机查询：可能是明确查询 / 指代查询 / 时间查询 / 宽泛查询。"""
    person = rng.choice(NAMES)
    location = rng.choice(LOCATIONS)
    event = rng.choice(EVENTS)
    roll = rng.random()
    if roll < 0.25:
        return f"{person}{event}的时候是不是在那个{location}"
    if roll < 0.4:
        return "她后来怎么样了"
    if roll < 0.55:
        return "我们上周说的那件事后来呢"
    if roll < 0.7:
        return "把所有相关细节都完整地讲一遍吧"
    if roll < 0.85:
        return f"{person}在{location}还提到过什么"
    return f"{person}{event}后到底发生了什么，从头到尾"


if __name__ == "__main__":
    unittest.main()
