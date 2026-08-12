from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.provider import ProviderRequest

from astrbot_plugin_memos_memory.body_rhythm import (
    calculate_body_state,
    render_body_context,
)
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.vector_store import VectorStore
from astrbot_plugin_memos_memory.xinchao import XinchaoController, validate_settings
from astrbot_plugin_memos_memory.xinchao_engine import (
    add_flash_thought,
    apply_conversation_event,
    bark_allowed,
    breath_dream_context,
    daytime_emergence_allowed,
    dream_allowed,
    effective_drive_value,
    message_similarity,
    new_thought_pool,
    new_state,
    normalize_state,
    proactive_allowed,
    recent_bark_history,
    recent_daytime_memory_keys,
    record_bark,
    record_daytime_emergence,
    record_dream,
    schedule_daytime_emergence,
    settle_state,
    tick_thought_pool,
)
from astrbot_plugin_memos_memory.xinchao_store import StateStore
from astrbot_plugin_memos_memory.webui import (
    WebUIServer,
    _is_preset_protected_setting,
    _safe_preset_values,
)


class FakeContext:
    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self, umo=None):
        return None

    def get_all_providers(self):
        return []


class SavableConfig(dict):
    def save_config(self):
        self["_saved_for_test"] = True


class FakeProvider:
    def __init__(self, responses=None, delay=0, provider_id="test_llm", model="test-model"):
        self.responses = list(responses or [])
        self.delay = delay
        self.provider_id = provider_id
        self.model = model
        self.calls = 0
        self.requests = []

    def meta(self):
        return SimpleNamespace(id=self.provider_id, model=self.model)

    async def text_chat(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.responses.pop(0) if self.responses else ""
        if isinstance(text, BaseException):
            raise text
        return SimpleNamespace(completion_text=text)


class ProviderContext(FakeContext):
    def __init__(self, provider):
        self.provider = provider
        self.sent = []

    def get_provider_by_id(self, provider_id):
        return self.provider

    def get_using_provider(self, umo=None):
        return self.provider

    def get_all_providers(self):
        return [self.provider]

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


class FakePlugin:
    character_name = "测试角色"
    _vec = None

    def __init__(self, context=None):
        self.context = context or FakeContext()
        self.logs = []

    def _log_event(self, category, message, detail):
        self.logs.append((category, message, detail))


class FakeVec:
    def __init__(self, rows):
        self.rows = list(rows)

    def list_memories(self, limit=80):
        return self.rows[:limit]


class FakeEvent:
    def __init__(self, text="你好", umo="default:friend"):
        self.message_str = text
        self.unified_msg_origin = umo
        self.extra = {}

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)


class BodyRhythmTests(unittest.TestCase):
    def settings(self, **overrides):
        value = {
            "body_rhythm_enable": True,
            "body_anchor_date": "2026-07-01",
            "body_cycle_length": 28,
            "body_period_length": 5,
            "body_ovulation_day": 14,
            "body_ovulation_window": 3,
            "body_effect_strength": 1.0,
            "body_daily_variation": 0.0,
            "body_expression_mode": "balanced",
            "body_time_modulation": False,
            "body_energy_scale": 1.0,
            "body_discomfort_scale": 1.0,
            "body_sensitivity_scale": 1.0,
            "body_closeness_influence": True,
            "body_closeness_scale": 1.0,
            "time_zone": "UTC",
        }
        value.update(overrides)
        return value

    def state_at(self, day: int, **overrides):
        return calculate_body_state(
            self.settings(**overrides),
            datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc),
            "测试角色",
        )

    def test_four_phase_boundaries(self):
        self.assertEqual(self.state_at(1)["phase"], "menstrual")
        self.assertEqual(self.state_at(1)["daysToNextCycle"], 28)
        self.assertEqual(self.state_at(6)["phase"], "follicular")
        self.assertEqual(self.state_at(13)["phase"], "ovulatory")
        self.assertEqual(self.state_at(16)["phase"], "luteal")
        self.assertEqual(self.state_at(28)["daysToNextCycle"], 1)

    def test_daily_variation_is_stable_and_bounded(self):
        settings = self.settings(body_daily_variation=1.0)
        now = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
        first = calculate_body_state(settings, now, "测试角色")
        second = calculate_body_state(settings, now, "测试角色")
        other = calculate_body_state(settings, now + timedelta(days=1), "测试角色")
        self.assertEqual(first["signals"], second["signals"])
        self.assertNotEqual(first["signals"], other["signals"])
        self.assertTrue(all(0 <= value <= 1 for value in first["signals"].values()))

    def test_missing_anchor_is_a_clean_noop(self):
        state = calculate_body_state(self.settings(body_anchor_date=""), character_key="测试角色")
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "anchor_missing")
        self.assertEqual(render_body_context(state), [])

    def test_neutral_body_state_is_not_injected(self):
        state = self.state_at(20, body_expression_mode="significant")
        self.assertTrue(state["available"])
        self.assertEqual(state["tendencies"], [])
        self.assertEqual(render_body_context(state), [])

    def test_balanced_mode_keeps_a_subtle_subjective_baseline(self):
        state = self.state_at(20)
        lines = render_body_context(state)
        self.assertTrue(lines)
        self.assertIn("主体感受", "\n".join(lines))
        self.assertEqual(state["expressionMode"], "balanced")

    def test_phase_specific_time_modifiers_change_signal_and_experience(self):
        settings = self.settings(body_time_modulation=True)
        morning = calculate_body_state(
            settings,
            datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc),
            "测试角色",
        )
        afternoon = calculate_body_state(
            settings,
            datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
            "测试角色",
        )
        self.assertEqual(morning["timeBand"], "morning")
        self.assertEqual(afternoon["timeBand"], "afternoon")
        self.assertNotEqual(morning["timeExperience"], afternoon["timeExperience"])
        self.assertGreater(
            morning["signals"]["discomfort"],
            afternoon["signals"]["discomfort"],
        )

    def test_every_phase_and_time_band_has_an_embodied_description(self):
        samples = {
            "menstrual": 1,
            "follicular": 7,
            "ovulatory": 14,
            "luteal": 20,
        }
        hours = {"morning": 7, "afternoon": 14, "evening": 20, "deep_night": 1}
        for phase, day in samples.items():
            for band, hour in hours.items():
                state = calculate_body_state(
                    self.settings(body_time_modulation=True),
                    datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc),
                    "测试角色",
                )
                self.assertEqual(state["phase"], phase)
                self.assertEqual(state["timeBand"], band)
                self.assertTrue(state["phaseExperience"])
                self.assertTrue(state["timeExperience"])
                self.assertTrue(state["injectionPreview"])

    def test_rendered_body_context_hides_cycle_labels_and_numbers(self):
        lines = render_body_context(self.state_at(1))
        text = "\n".join(lines)
        self.assertIn("当前身体底色", text)
        self.assertIn("不能自动制造", text)
        self.assertNotIn("经期", text)
        self.assertNotIn("排卵", text)
        self.assertNotIn("周期第", text)

    def test_body_signals_are_continuous_across_midnight(self):
        settings = self.settings(body_daily_variation=1.0, body_time_modulation=True)
        before = calculate_body_state(
            settings,
            datetime(2026, 7, 5, 23, 59, tzinfo=timezone.utc),
            "测试角色",
        )
        after = calculate_body_state(
            settings,
            datetime(2026, 7, 6, 0, 1, tzinfo=timezone.utc),
            "测试角色",
        )
        largest_jump = max(
            abs(before["signals"][key] - after["signals"][key])
            for key in before["signals"]
        )
        self.assertLess(largest_jump, 0.02)

    def test_body_signals_are_continuous_across_time_band_boundary(self):
        settings = self.settings(body_daily_variation=0.0, body_time_modulation=True)
        before = calculate_body_state(
            settings,
            datetime(2026, 7, 7, 11, 59, tzinfo=timezone.utc),
            "测试角色",
        )
        after = calculate_body_state(
            settings,
            datetime(2026, 7, 7, 12, 1, tzinfo=timezone.utc),
            "测试角色",
        )
        largest_jump = max(
            abs(before["signals"][key] - after["signals"][key])
            for key in before["signals"]
        )
        self.assertLess(largest_jump, 0.02)


class EngineTests(unittest.TestCase):
    def test_growth_satisfaction_and_sleep_wake(self):
        start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(start)
        settled, meta = settle_state(
            state,
            start + timedelta(hours=2),
            sleep_after_minutes=60,
            time_zone="UTC",
            dawn_start=1,
            dawn_end=8,
        )
        self.assertTrue(meta["enteredSleep"])
        self.assertEqual(settled["consciousness"], "sleeping")
        self.assertGreater(settled["drives"]["social"], state["drives"]["social"])

        dreamed = record_dream(
            settled,
            residue="雨声仍在",
            awareness="我还在意那次等待",
            dream_text="走廊尽头一直亮着灯",
            now=start + timedelta(hours=2, minutes=1),
        )
        awake, wake_meta = apply_conversation_event(
            dreamed,
            {"satisfiedDrives": ["social"], "summary": "再次见面"},
            now=start + timedelta(hours=2, minutes=2),
            umo="default:friend",
        )
        self.assertTrue(wake_meta["wasSleeping"])
        self.assertEqual(awake["consciousness"], "awake")
        self.assertEqual(awake["pendingAwareness"]["residue"], "雨声仍在")
        self.assertLess(awake["drives"]["social"], settled["drives"]["social"])

    def test_dream_limits(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(now)
        state["consciousness"] = "sleeping"
        self.assertTrue(dream_allowed(state, now, 6, 3))
        state = record_dream(state, "余韵", now=now)
        self.assertFalse(dream_allowed(state, now + timedelta(hours=5), 6, 3))
        self.assertTrue(dream_allowed(state, now + timedelta(hours=7), 6, 3))

    def test_dream_context_and_cross_type_active_history(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(now - timedelta(hours=2))
        state = record_dream(
            state,
            "安静",
            awareness="醒来只记得一盏灯",
            now=now - timedelta(hours=1),
        )
        context = breath_dream_context(state, now, 18, 3)
        self.assertTrue(context["available"])
        self.assertEqual(context["dreams"][0]["summary"], "醒来只记得一盏灯")
        self.assertNotIn("dream", context["dreams"][0])

        state = record_bark(state, "突然想摸你的头发。", "dream", now)
        state = record_bark(
            state, "今天窗外的云压得很低。", "autonomous_thought",
            now + timedelta(hours=1),
        )
        self.assertEqual(
            [item["kind"] for item in recent_bark_history(state)],
            ["dream", "autonomous_thought"],
        )
        self.assertGreaterEqual(
            message_similarity("突然想摸你的头发。", "刚刚又想摸你的头发"),
            0.55,
        )
        self.assertFalse(
            bark_allowed(state, now + timedelta(hours=2), 3, 6, "dream"),
        )

    def test_daytime_emergence_schedule_and_window(self):
        start = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
        state = schedule_daytime_emergence(
            new_state(start), start, 2, 3, rng=lambda: 0.5,
        )
        due = start + timedelta(hours=2, minutes=30)
        self.assertFalse(
            daytime_emergence_allowed(state, due - timedelta(seconds=1), "Asia/Shanghai", 8, 23, 7),
        )
        self.assertTrue(
            daytime_emergence_allowed(state, due, "Asia/Shanghai", 8, 23, 7),
        )
        state = record_daytime_emergence(
            state,
            "忽然想到昨天没说完的话。",
            due,
            "Asia/Shanghai",
            ["memos/day-1"],
        )
        self.assertEqual(state["daytimeEmergenceUsage"]["2026-07-20"], 1)
        self.assertEqual(recent_bark_history(state)[-1]["kind"], "daytime_emergence")
        self.assertEqual(
            recent_daytime_memory_keys(state, due, 72),
            {"memos/day-1"},
        )
        self.assertEqual(
            recent_daytime_memory_keys(state, due + timedelta(hours=73), 72),
            set(),
        )

    def test_old_state_is_extended_without_losing_values(self):
        old = {
            "schemaVersion": 2,
            "revision": 17,
            "consciousness": "awake",
            "drives": {"social": 0.72},
            "thoughtPool": {"flash": [], "obsessions": []},
            "lastProactiveAt": "2026-07-20T01:00:00Z",
            "recentProactiveMessages": [
                {"at": "2026-07-20T01:00:00Z", "message": "旧版主动消息"},
            ],
        }
        migrated = normalize_state(old)
        self.assertEqual(migrated["revision"], 17)
        self.assertEqual(migrated["drives"]["social"], 0.72)
        self.assertIn("recentBarkMessages", migrated)
        self.assertIn("daytimeEmergenceUsage", migrated)
        self.assertIn("recentDaytimeMemories", migrated)
        self.assertEqual(migrated["recentBarkMessages"][0]["message"], "旧版主动消息")
        self.assertEqual(migrated["schemaVersion"], 6)
        self.assertEqual(migrated["driveActivations"]["social"], 0.0)
        self.assertIn("social", migrated["driveMeta"])

    def test_pressure_activation_and_satisfaction_are_distinct(self):
        start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(start)
        updated, _ = apply_conversation_event(
            state,
            {
                "activatedDrives": ["social"],
                "activationLevels": {"social": 0.50},
                "satisfactionLevels": {"social": 0.30},
            },
            now=start,
        )
        self.assertAlmostEqual(updated["drives"]["social"], 0.123, places=3)
        self.assertGreater(updated["driveActivations"]["social"], 0.45)
        self.assertGreater(
            effective_drive_value(updated, "social"),
            updated["drives"]["social"],
        )

    def test_repeated_normal_conversation_does_not_zero_social_need(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(now)
        for _ in range(4):
            state, _ = apply_conversation_event(
                state,
                {
                    "activationLevels": {"social": 0.50},
                    "satisfactionLevels": {"social": 0.30},
                },
                now=now,
            )
        self.assertGreater(state["drives"]["social"], 0.05)
        self.assertGreater(state["driveActivations"]["social"], 0.40)

    def test_affect_pressure_and_activation_decay_naturally(self):
        start = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(start)
        state["drives"]["grieve"] = 0.8
        state["driveActivations"]["grieve"] = 0.8
        settled, _ = settle_state(
            state,
            start + timedelta(hours=36),
            sleep_after_minutes=9999,
            time_zone="UTC",
        )
        self.assertAlmostEqual(settled["drives"]["grieve"], 0.4, places=2)
        self.assertAlmostEqual(settled["driveActivations"]["grieve"], 0.1, places=2)

    def test_reinforced_flash_thought_becomes_obsession(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        pool = new_thought_pool()
        for _ in range(3):
            add_flash_thought(pool, "monitor", "他是不是还有话没有说完", 0.58, now)
        tick_thought_pool(pool, 0.25, now + timedelta(minutes=15))
        self.assertFalse(pool["flash"])
        self.assertEqual(pool["obsessions"][0]["key"], "monitor")
        self.assertGreaterEqual(pool["obsessions"][0]["reinforcements"], 3)

    def test_thought_decay_depends_on_elapsed_time_not_chat_count(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        once = new_thought_pool()
        often = new_thought_pool()
        add_flash_thought(once, "reflection", "那句话值得再想一想", 0.70, now)
        add_flash_thought(often, "reflection", "那句话值得再想一想", 0.70, now)
        tick_thought_pool(once, 2.0, now + timedelta(hours=2))
        for index in range(8):
            tick_thought_pool(
                often,
                0.25,
                now + timedelta(minutes=15 * (index + 1)),
            )
        self.assertAlmostEqual(
            once["flash"][0]["intensity"],
            often["flash"][0]["intensity"],
            places=3,
        )

    def test_current_activation_can_open_proactive_gate(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        state = new_state(now - timedelta(hours=8))
        state["consciousness"] = "sleeping"
        state["lastUmo"] = "default:friend"
        state["driveActivations"]["social"] = 1.0
        self.assertTrue(proactive_allowed(state, now, 6, 6, 3, 0.58))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_persistence_and_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = StateStore(path)
            updated = await store.update(
                "character:test",
                lambda state: {**state, "fatigue": 0.2, "revision": 9},
            )
            self.assertEqual(updated["revision"], 9)
            reopened = StateStore(path)
            loaded = await reopened.read("character:test")
            self.assertEqual(loaded["fatigue"], 0.2)
            reset = await reopened.reset("character:test")
            self.assertEqual(reset["revision"], 0)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin()
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()

    async def asyncTearDown(self):
        await self.controller.terminate()
        self.temp.cleanup()

    async def test_injection_and_rule_perception(self):
        event = FakeEvent("我有点难过，你会陪着我吗？")
        request = ProviderRequest(prompt=event.message_str)
        await self.controller.add_thought(
            "",
            "grieve",
            "这句话让我担心彼此会不会走散",
            0.7,
        )
        await self.controller.on_request(event, request)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        block = request.extra_user_content_parts[0].text
        self.assertIn("<DynamicMindState", block)
        self.assertNotIn("0.15", block)

        await self.controller._perceive_and_apply(
            "character:测试角色",
            event.unified_msg_origin,
            event.message_str,
            "我会在这里陪着你。",
        )
        state = await self.controller.status()
        self.assertGreater(state["drives"][-2]["value"], 0)
        self.assertTrue(state["lastPerception"]["source"].startswith("rules"))
        self.assertTrue(state["recentEvents"])

    async def test_current_turn_appraisal_is_injected_before_response(self):
        event = FakeEvent("爱莉，我们没有可能了。")
        request = ProviderRequest(prompt=event.message_str)
        before = await self.controller.status()
        await self.controller.on_request(event, request)
        block = request.extra_user_content_parts[-1].text
        appraisal = event.get_extra("xinchao_live_appraisal")
        after = await self.controller.status()

        self.assertIn("本轮输入刚刚触发", block)
        self.assertIn("失去", block)
        self.assertIn("grieve", appraisal["activatedDrives"])
        # Request-time appraisal is transient; persistent deltas are applied only
        # after the actual assistant response is available.
        self.assertEqual(
            next(item["value"] for item in before["drives"] if item["key"] == "grieve"),
            next(item["value"] for item in after["drives"] if item["key"] == "grieve"),
        )

    async def test_rule_appraisal_does_not_treat_apology_as_hug(self):
        appraisal = self.controller._rule_appraisal("抱歉，我刚才说错了。")
        self.assertNotIn("crave", appraisal["activatedDrives"])
        self.assertNotIn("possess", appraisal["activatedDrives"])

    def test_user_sharing_does_not_satisfy_character_share_drive(self):
        event = self.controller._rule_event(
            "我今天发现了一件很有趣的事，想分享给你。",
            "我在听，你慢慢说。",
        )
        self.assertNotIn("share", event["satisfactionLevels"])

    def test_character_self_disclosure_satisfies_share_drive(self):
        event = self.controller._rule_event(
            "你今天过得怎么样？",
            "我今天其实一直在想昨晚那场雨，也想告诉你我的感受。",
        )
        self.assertGreaterEqual(event["satisfactionLevels"]["share"], 0.4)

    def test_mind_body_bridge_preserves_desire_under_low_social_energy(self):
        state = new_state()
        state["drives"]["social"] = 0.70
        bridge = self.controller._mind_body_bridge(
            state,
            {
                "available": True,
                "signals": {
                    "social_energy": 0.30,
                    "energy": 0.60,
                    "comfort_need": 0.40,
                    "sensitivity": 0.40,
                },
            },
        )
        self.assertIn("仍想保持连接", bridge)
        self.assertIn("不是降低在意", bridge)

    async def test_simulation_does_not_write_state(self):
        before = await self.controller.status()
        preview = await self.controller.simulate(
            "",
            12,
            {
                "satisfiedDrives": ["social"],
                "driveDeltas": {"grieve": 0.1},
                "flashThoughts": [],
            },
        )
        after = await self.controller.status()
        self.assertIn("simulation", preview)
        self.assertEqual(before["revision"], after["revision"])

    async def test_first_wake_request_receives_dream_residue(self):
        key = "character:测试角色"
        await self.controller.store.update(
            key,
            lambda state: record_dream(
                {
                    **state,
                    "consciousness": "sleeping",
                    "sleepStartedAt": datetime.now(timezone.utc).isoformat(),
                },
                residue="窗边的雨声还留在心里",
                awareness="我仍然在意那次等待",
                dream_text="灯一直没有熄灭",
            ),
        )
        event = FakeEvent("醒醒，我来找你了")
        request = ProviderRequest(prompt=event.message_str)
        await self.controller.on_request(event, request)
        block = request.extra_user_content_parts[-1].text
        self.assertIn("窗边的雨声", block)
        self.assertIn("我仍然在意那次等待", block)
        state = await self.controller.status()
        self.assertEqual(state["consciousness"], "awake")
        self.assertIsNone(state["pendingAwareness"])

    async def test_background_perception_never_blocks_response(self):
        event = FakeEvent("只是普通的一句话")
        response = SimpleNamespace(is_chunk=False, completion_text="我听到了。")
        await self.controller.on_response(event, response)
        await asyncio.sleep(0.05)
        state = await self.controller.status()
        self.assertIsNotNone(state["lastPerception"])

    def test_settings_validation(self):
        cfg = validate_settings(
            {
                "scope": "invalid",
                "injection_max_chars": 99999,
                "proactive_enable": True,
                "perception_min_confidence": -1,
                "body_cycle_length": 20,
                "body_period_length": 99,
                "body_ovulation_day": 99,
                "body_effect_strength": 2,
                "body_expression_mode": "invalid",
                "daytime_memory_cooldown_hours": 9999,
            }
        )
        self.assertEqual(cfg["scope"], "character")
        self.assertEqual(cfg["injection_max_chars"], 2400)
        self.assertEqual(cfg["perception_min_confidence"], 0.4)
        self.assertEqual(cfg["live_perception_timeout_seconds"], 15)
        self.assertEqual(cfg["post_perception_timeout_seconds"], 60)
        self.assertTrue(cfg["proactive_enable"])
        self.assertEqual(cfg["body_cycle_length"], 21)
        self.assertEqual(cfg["body_period_length"], 10)
        self.assertEqual(cfg["body_ovulation_day"], 19)
        self.assertEqual(cfg["body_effect_strength"], 1)
        self.assertEqual(cfg["body_expression_mode"], "balanced")
        self.assertEqual(cfg["daytime_memory_cooldown_hours"], 720)

        migrated = validate_settings({"perception_timeout_seconds": 20})
        self.assertEqual(migrated["live_perception_timeout_seconds"], 15)
        self.assertEqual(migrated["post_perception_timeout_seconds"], 60)
        custom = validate_settings({"perception_timeout_seconds": 90})
        self.assertEqual(custom["post_perception_timeout_seconds"], 90)
        old_live_default = validate_settings({"live_perception_timeout_seconds": 10})
        self.assertEqual(old_live_default["live_perception_timeout_seconds"], 15)
        explicit_live_custom = validate_settings({
            "settings_schema_version": 2,
            "live_perception_timeout_seconds": 10,
        })
        self.assertEqual(explicit_live_custom["live_perception_timeout_seconds"], 10)

    async def test_provider_options_use_astr_chat_provider_instances(self):
        await self.controller.terminate()
        provider = FakeProvider(provider_id="openai_2/deepseek-v4-pro", model="deepseek-v4-pro")
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.assertEqual(
            self.controller.provider_options(),
            [{
                "id": "openai_2/deepseek-v4-pro",
                "model": "deepseek-v4-pro",
                "label": "openai_2/deepseek-v4-pro · deepseek-v4-pro",
            }],
        )

    async def test_body_state_is_composed_into_one_temporary_mind_block(self):
        self.controller.settings.update({
            "body_rhythm_enable": True,
            "body_anchor_date": datetime.now(timezone.utc).date().isoformat(),
            "body_daily_variation": 0,
            "body_time_modulation": False,
            "time_zone": "UTC",
        })
        event = FakeEvent("只是普通问候")
        request = ProviderRequest(prompt=event.message_str)
        await self.controller.on_request(event, request)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        block = request.extra_user_content_parts[0].text
        self.assertIn("<DynamicMindState", block)
        self.assertIn("当前身体底色", block)
        self.assertNotIn("<CurrentBodyState", block)
        self.assertNotIn("经期第", block)
        status = await self.controller.status()
        self.assertTrue(status["bodyState"]["available"])
        self.assertTrue(status["lastInjection"]["diagnostics"]["body_available"])
        self.assertTrue(status["lastInjection"]["diagnostics"]["body_injected"])
        self.assertGreater(status["lastInjection"]["diagnostics"]["body_chars"], 0)
        self.assertIn(
            "当前身体底色",
            status["lastInjection"]["diagnostics"]["body_context"],
        )

    def test_body_state_is_consistent_across_session_scopes(self):
        self.controller.settings.update({
            "scope": "session",
            "body_rhythm_enable": True,
            "body_anchor_date": "2026-07-01",
            "body_daily_variation": 1,
            "body_time_modulation": False,
            "time_zone": "UTC",
        })
        now = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
        first = self.controller._body_state(now, "session:first")
        second = self.controller._body_state(now, "session:second")
        self.assertEqual(first["signals"], second["signals"])

    async def test_malformed_live_llm_falls_back_to_rules(self):
        await self.controller.terminate()
        provider = FakeProvider(["not-json", "still-not-json"])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(appraisal["_source"], "rules_fallback")
        self.assertEqual(appraisal["_diagnostics"]["reason"], "parse_error")
        self.assertIn("grieve", appraisal["activatedDrives"])

    async def test_live_format_failure_recovers_once_inside_same_budget(self):
        await self.controller.terminate()
        provider = FakeProvider([
            "not-json",
            '{"activatedDrives":["monitor"],"confidence":0.9}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "你怎么突然不说话了？",
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(appraisal["_source"], "llm")
        self.assertTrue(appraisal["_diagnostics"]["recovered"])
        self.assertEqual(appraisal["_diagnostics"]["recovered_from"], "parse_error")
        self.assertEqual(appraisal["activationLevels"]["monitor"], 0.5)
        self.assertIn("上一次输出未通过结构校验", provider.requests[1]["prompt"])

    async def test_live_schema_failures_do_not_open_transport_circuit(self):
        await self.controller.terminate()
        provider = FakeProvider(["bad", "still bad"] * 4)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        for _ in range(3):
            await self.controller._appraise_current_input(
                "default:friend", "我们没有可能了。",
            )
        health = self.controller._perception_health_payload("live")
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["consecutive_soft_failures"], 3)
        calls_before = provider.calls
        await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(provider.calls, calls_before + 2)

    async def test_live_provider_error_is_not_retried_before_response(self):
        await self.controller.terminate()
        provider = FakeProvider([RuntimeError("provider down"), "unused"])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(appraisal["_source"], "rules_fallback")
        self.assertEqual(appraisal["_diagnostics"]["reason"], "provider_error")

    async def test_live_llm_accepts_fenced_repairable_json_and_aliases(self):
        await self.controller.terminate()
        provider = FakeProvider([
            "说明如下：\n```json\n{"
            "'activated_drives':['难过与失落'],"
            "'activation_levels':{'难过与失落':0.82},"
            "'drive_deltas':{'grieve':0.1},"
            "'flash_thoughts':[{'drive':'难过与失落','content':'这句话仍在心里回响','score':0.7}],"
            "'guidance':'先承接关系受损的重量','confidence':'86%',}\n```",
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(appraisal["_source"], "llm")
        self.assertEqual(appraisal["_diagnostics"]["attempts"], 1)
        self.assertIn("grieve", appraisal["activatedDrives"])
        # Explicit relationship-break evidence from the deterministic guard is
        # retained as a floor even when the LLM gives a slightly lower level.
        self.assertAlmostEqual(appraisal["activationLevels"]["grieve"], 0.88)
        self.assertAlmostEqual(appraisal["confidence"], 0.86)

    async def test_background_llm_retries_format_failure_within_one_budget(self):
        await self.controller.terminate()
        provider = FakeProvider([
            "not-json",
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"正常交流",'
            '"confidence":0.9}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
        )
        status = await self.controller.status("character:测试角色")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(status["lastPerception"]["source"], "llm")
        self.assertEqual(status["lastPerception"]["diagnostics"]["attempts"], 2)
        self.assertEqual(status["perceptionHealth"]["state"], "healthy")

    async def test_perception_circuit_breaker_skips_repeated_broken_provider(self):
        await self.controller.terminate()
        provider = FakeProvider([RuntimeError("down")] * 6)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        for _ in range(3):
            appraisal = await self.controller._appraise_current_input(
                "default:friend", "我们没有可能了。",
            )
            self.assertEqual(appraisal["_source"], "rules_fallback")
        calls_after_open = provider.calls
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(provider.calls, calls_after_open)
        self.assertEqual(appraisal["_diagnostics"]["reason"], "circuit_open")
        health = self.controller._perception_health_payload("live")
        self.assertEqual(health["state"], "circuit_open")

    async def test_explicit_missing_provider_does_not_silently_follow_session(self):
        await self.controller.terminate()
        provider = FakeProvider()

        class MissingConfiguredContext(ProviderContext):
            def get_provider_by_id(self, provider_id):
                return None

        self.plugin = FakePlugin(MissingConfiguredContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["live_perception_provider_id"] = "removed-provider"
        appraisal = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(appraisal["_source"], "rules_no_provider")

    async def test_live_llm_timeout_is_bounded_and_falls_back(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"activatedDrives":["grieve"],"confidence":0.99}',
        ], delay=0.08)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["live_perception_timeout_seconds"] = 0.01
        appraisal = await asyncio.wait_for(
            self.controller._appraise_current_input(
                "default:friend", "我们没有可能了。",
            ),
            timeout=0.2,
        )
        self.assertEqual(appraisal["_source"], "rules_fallback")
        self.assertIn("grieve", appraisal["activatedDrives"])

    async def test_live_failure_does_not_open_post_circuit(self):
        await self.controller.terminate()
        valid_post = (
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"正常交流",'
            '"confidence":0.9}'
        )
        provider = FakeProvider([RuntimeError("live down")] * 3 + [valid_post])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        for _ in range(3):
            await self.controller._appraise_current_input(
                "default:friend", "我们没有可能了。",
            )
        self.assertEqual(
            self.controller._perception_health_payload("live")["state"],
            "circuit_open",
        )
        self.controller.settings["perception_mode"] = "llm"
        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
        )
        status = await self.controller.status("character:测试角色")
        self.assertEqual(status["lastPerception"]["source"], "llm")
        self.assertEqual(status["perceptionHealthByStage"]["post"]["state"], "healthy")
        self.assertEqual(status["perceptionHealthByStage"]["live"]["state"], "circuit_open")

    async def test_live_and_post_use_independent_timeout_budgets(self):
        await self.controller.terminate()
        valid_post = (
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"正常交流",'
            '"confidence":0.9}'
        )
        provider = FakeProvider([valid_post], delay=0.08)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["live_perception_timeout_seconds"] = 0.01
        self.controller.settings["post_perception_timeout_seconds"] = 0.2
        live = await self.controller._appraise_current_input(
            "default:friend", "我们没有可能了。",
        )
        self.assertEqual(live["_source"], "rules_fallback")
        self.controller.settings["perception_mode"] = "llm"
        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
        )
        status = await self.controller.status("character:测试角色")
        self.assertEqual(status["lastPerception"]["source"], "llm")
        self.assertEqual(status["perceptionHealthByStage"]["live"]["last_error_kind"], "timeout")
        self.assertEqual(status["perceptionHealthByStage"]["post"]["state"], "healthy")

    async def test_live_and_post_use_their_configured_providers(self):
        await self.controller.terminate()
        live_provider = FakeProvider([
            '{"activatedDrives":["monitor"],"activationLevels":{"monitor":0.7},'
            '"driveDeltas":{},"flashThoughts":[],"guidance":"先观察",'
            '"confidence":0.9}',
        ], provider_id="live-provider")
        post_provider = FakeProvider([
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"正常交流",'
            '"confidence":0.9}',
        ], provider_id="post-provider")

        class SplitProviderContext(FakeContext):
            def get_provider_by_id(self, provider_id):
                return {
                    "live-provider": live_provider,
                    "post-provider": post_provider,
                }.get(provider_id)

            def get_using_provider(self, umo=None):
                return live_provider

            def get_all_providers(self):
                return [live_provider, post_provider]

        self.plugin = FakePlugin(SplitProviderContext())
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["live_perception_provider_id"] = "live-provider"
        self.controller.settings["perception_provider_id"] = "post-provider"

        live = await self.controller._llm_live_appraisal(
            "default:friend", "你怎么突然不说话了？",
        )
        post = await self.controller._llm_perception(
            "default:friend", "晚上好。", "晚上好，我在这里。",
        )

        self.assertEqual(live["activatedDrives"], ["monitor"])
        self.assertEqual(post["activatedDrives"], ["social"])
        self.assertEqual(live_provider.calls, 1)
        self.assertEqual(post_provider.calls, 1)
        self.assertEqual(
            self.controller._perception_health_payload("live")["provider"],
            "live-provider",
        )
        self.assertEqual(
            self.controller._perception_health_payload("post")["provider"],
            "post-provider",
        )

    async def test_missing_post_provider_does_not_block_or_use_live_provider(self):
        await self.controller.terminate()
        live_provider = FakeProvider(provider_id="live-provider")

        class MissingPostContext(FakeContext):
            def get_provider_by_id(self, provider_id):
                return live_provider if provider_id == "live-provider" else None

            def get_using_provider(self, umo=None):
                return live_provider

        self.plugin = FakePlugin(MissingPostContext())
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        self.controller.settings["live_perception_provider_id"] = "live-provider"
        self.controller.settings["perception_provider_id"] = "removed-post-provider"

        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
        )

        status = await self.controller.status("character:测试角色")
        self.assertEqual(status["lastPerception"]["source"], "rules_no_provider")
        self.assertEqual(status["lastPerception"]["diagnostics"]["reason"], "provider_missing")
        self.assertEqual(live_provider.calls, 0)

    async def test_burst_turns_coalesce_into_one_background_call(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"satisfiedDrives":["social"],"satisfactionLevels":{"social":0.3},'
            '"activatedDrives":[],"activationLevels":{},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"连续闲聊",'
            '"confidence":0.9}'
        ] * 6, delay=0.02)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        for index in range(4):
            event = FakeEvent(f"第{index + 1}句话")
            response = SimpleNamespace(is_chunk=False, completion_text=f"回应{index + 1}")
            await self.controller.on_response(event, response)
        for _ in range(80):
            if not self.controller._pending_turns.get("character:测试角色"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertLess(provider.calls, 4)
        status = await self.controller.status("character:测试角色")
        self.assertIsNotNone(status["lastPerception"])
        self.assertGreaterEqual(status["lastPerception"]["mergedTurns"], 1)
        self.assertEqual(status["perceptionQueue"]["pending"], 0)

    async def test_non_coalesced_mode_keeps_every_turn(self):
        """4.5.3-beta: 关闭合并时严格 FIFO 逐条结算，不丢消息（修复 4.5.2 静默清队）。"""
        await self.controller.terminate()
        provider = FakeProvider([
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"逐条",'
            '"confidence":0.9}'
        ] * 3, delay=0.01)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        self.controller.settings["post_perception_coalesce_enable"] = False
        key = "character:测试角色"
        for index in range(3):
            self.controller._enqueue_perception(
                key, "default:friend", f"第{index + 1}句", f"回应{index + 1}", None,
            )
        for _ in range(150):
            if not self.controller._pending_turns.get(key):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(self.controller._pending_turns.get(key), [])

    async def test_worker_slot_released_before_task_done(self):
        """4.5.3-beta: worker 退出后槽位立即释放，新消息立即拉起新 worker（修复 4.5.2 滞留竞态）。"""
        await self.controller.terminate()
        provider = FakeProvider([
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"连续",'
            '"confidence":0.9}'
        ] * 3, delay=0.01)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        key = "character:测试角色"
        response = SimpleNamespace(is_chunk=False, completion_text="回应")
        await self.controller.on_response(FakeEvent("第一句"), response)
        for _ in range(150):
            if not self.controller._pending_turns.get(key) and not self.controller._perception_workers.get(key):
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(key, self.controller._perception_workers)
        await self.controller.on_response(FakeEvent("第二句"), response)
        self.assertIn(key, self.controller._perception_workers)
        for _ in range(150):
            if not self.controller._pending_turns.get(key):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.controller._pending_turns.get(key), [])

    async def test_coalesced_batch_prompt_carries_every_merged_turn(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"连续互动",'
            '"confidence":0.9}'
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        parsed = await self.controller._llm_perception(
            "default:friend",
            ["第一句", "第二句"],
            ["第一答", "第二答"],
        )
        prompt = provider.requests[0]["prompt"]
        self.assertEqual(parsed["activatedDrives"], ["social"])
        for fragment in ("第一句", "第二句", "第一答", "第二答"):
            self.assertIn(fragment, prompt)
        self.assertIn("2 轮对话", prompt)

    async def test_open_post_circuit_skips_prompt_construction(self):
        await self.controller.terminate()
        provider = FakeProvider([RuntimeError("post down")] * 12)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        for _ in range(3):
            await self.controller._perceive_and_apply(
                "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
            )
        calls_after_open = provider.calls
        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend", "晚上好。", "晚上好，我在这里。",
        )
        status = await self.controller.status("character:测试角色")
        self.assertEqual(provider.calls, calls_after_open)
        self.assertEqual(status["lastPerception"]["diagnostics"]["reason"], "circuit_open")
        self.assertEqual(status["perceptionHealthByStage"]["post"]["state"], "circuit_open")

    async def test_repeated_circuit_trips_extend_the_cooldown(self):
        await self.controller.terminate()
        provider = FakeProvider([RuntimeError("down")] * 30)
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        for _ in range(3):
            self.controller._mark_perception_failure(provider, "post", "error", "down")
        first = self.controller._perception_health["post"]["circuit_backoff_seconds"]
        self.controller._mark_perception_failure(provider, "post", "error", "down")
        second = self.controller._perception_health["post"]["circuit_backoff_seconds"]
        self.assertEqual(second, first * 2)
        self.controller._mark_perception_success(provider, "post")
        self.assertEqual(
            self.controller._perception_health["post"]["circuit_backoff_seconds"], 0.0,
        )

    async def test_concurrent_settles_do_not_double_fire_generators(self):
        key = "character:测试角色"
        runs = 0

        async def fake_generators(scope, state):
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return state

        self.controller._run_generators = fake_generators
        await asyncio.gather(
            self.controller._settle_one(key, allow_generators=True),
            self.controller._settle_one(key, allow_generators=True),
        )
        self.assertEqual(runs, 1)

    async def test_generator_flag_cleared_when_generator_raises(self):
        """4.5.3-beta: 生成器异常时运行标志必须清除，后续结算不受影响。"""
        key = "character:测试角色"

        async def broken_generators(scope, state):
            raise RuntimeError("boom")

        self.controller._run_generators = broken_generators
        with self.assertRaises(RuntimeError):
            await self.controller._settle_one(key, allow_generators=True)
        self.assertNotIn(key, self.controller._generator_running)

        async def ok_generators(scope, state):
            return state

        self.controller._run_generators = ok_generators
        await self.controller._settle_one(key, allow_generators=True)
        self.assertNotIn(key, self.controller._generator_running)

    async def test_llm_perception_keeps_deterministic_event_evidence(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"satisfactionLevels":{},"activatedDrives":[],"driveDeltas":{},'
            '"flashThoughts":[],"confidence":0.95}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        await self.controller._perceive_and_apply(
            "character:测试角色",
            "default:friend",
            "爱莉，我们没有可能了。",
            "我听明白了，我会认真面对这句话。",
        )
        status = await self.controller.status("character:测试角色")
        self.assertNotIn("share", await self.controller.store.keys())
        self.assertNotIn("grieve", await self.controller.store.keys())
        event = status["lastPerception"]["event"]
        self.assertEqual(status["lastPerception"]["source"], "llm")
        self.assertGreater(event["driveDeltas"]["grieve"], 0)
        self.assertGreater(event["satisfactionLevels"]["social"], 0)
        self.assertTrue(event["flashThoughts"])

    def test_two_stage_reconciliation_resolves_or_carries_live_activation_once(self):
        live = {
            "activatedDrives": ["social", "monitor"],
            "activationLevels": {"social": 0.70, "monitor": 0.60},
        }
        event, diagnostics = self.controller._reconcile_two_stage_event(
            {
                "satisfactionLevels": {"social": 0.65},
                "activatedDrives": ["social", "monitor"],
                "activationLevels": {"social": 0.70, "monitor": 0.60},
            },
            live,
            declared_activations=set(),
        )
        self.assertNotIn("social", event["activatedDrives"])
        self.assertIn("monitor", event["activatedDrives"])
        self.assertEqual(diagnostics["resolved"], ["social"])
        self.assertEqual(diagnostics["carried"], ["monitor"])
        self.assertAlmostEqual(event["activationLevels"]["monitor"], 0.468)

    async def test_two_channels_share_time_state_and_live_evidence(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"activatedDrives":["monitor"],"activationLevels":{"monitor":0.7},'
            '"driveDeltas":{},"flashThoughts":[],"guidance":"先观察",'
            '"confidence":0.9}',
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["monitor"],"activationLevels":{"monitor":0.5},'
            '"driveDeltas":{},"flashThoughts":[],"summary":"仍在关注",'
            '"confidence":0.9}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin._semantic_state_status = lambda: {
            "state": {"rendered_text": "关系位置：愿意直接确认彼此状态。"},
        }
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["perception_mode"] = "llm"
        now = datetime(2026, 8, 9, 3, 4, 5, tzinfo=timezone.utc)
        live = await self.controller._appraise_current_input(
            "default:friend", "你怎么突然不说话了？", now=now,
        )
        await self.controller._perceive_and_apply(
            "character:测试角色", "default:friend",
            "你怎么突然不说话了？", "我在，只是在认真听你说。",
            live_appraisal=live,
        )
        self.assertIn("2026-08-09 11:04:05", provider.requests[0]["prompt"])
        self.assertIn("当前滚动人格状态", provider.requests[0]["prompt"])
        self.assertIn("回答前即时评估", provider.requests[1]["prompt"])
        self.assertIn('"monitor"', provider.requests[1]["prompt"])
        status = await self.controller.status("character:测试角色")
        self.assertTrue(status["lastPerception"]["diagnostics"]["two_stage"]["available"])

    async def test_partial_post_json_is_retried_instead_of_silently_accepted(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"activatedDrives":["social"],"confidence":0.9}',
            '{"satisfiedDrives":[],"satisfactionLevels":{},'
            '"activatedDrives":["social"],"activationLevels":{"social":0.4},'
            '"driveDeltas":{},"confidence":0.9}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        parsed = await self.controller._llm_perception(
            "default:friend", "晚上好。", "晚上好，我在。",
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(parsed["_llm_meta"]["attempts"], 2)

    async def test_cross_type_duplicate_is_regenerated_once(self):
        await self.controller.terminate()
        provider = FakeProvider([
            "突然想摸你的头发。",
            "今天窗外的云压得很低。",
        ])
        context = ProviderContext(provider)
        self.plugin = FakePlugin(context)
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        key = "character:测试角色"
        state = await self.controller.store.update(
            key,
            lambda current: {
                **record_bark(current, "突然想摸你的头发。", "dream"),
                "lastUmo": "default:friend",
            },
        )
        updated, sent = await self.controller._send_active_message(
            key, state, "autonomous_thought",
        )
        self.assertTrue(sent)
        self.assertEqual(provider.calls, 2)
        self.assertIn("分层心理状态", provider.requests[0]["prompt"])
        self.assertIn("身心合成", provider.requests[0]["prompt"])
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(
            recent_bark_history(updated)[-1]["message"],
            "今天窗外的云压得很低。",
        )

    async def test_daytime_memory_source_cooldown_survives_restart(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"memory_index":1,"message":"忽然想起那天窗边没说完的话。"}',
        ])
        context = ProviderContext(provider)
        self.plugin = FakePlugin(context)
        self.plugin._vec = FakeVec([{
            "memo_name": "memos/day-1",
            "preview": "那天两个人在窗边说了很久，最后一句话没有说完。",
            "ts_text": "2026-07-01",
            "memory_type": "relationship",
            "importance": 5,
            "event_ts": 100,
        }])
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        key = "character:测试角色"
        state = await self.controller.store.update(
            key,
            lambda current: {**current, "lastUmo": "default:friend"},
        )
        updated, sent = await self.controller._send_active_message(
            key, state, "daytime_emergence",
        )
        self.assertTrue(sent)
        self.assertEqual(provider.calls, 1)
        self.assertIn("此刻内在方向", provider.requests[0]["prompt"])
        self.assertIn("候选记忆与当前心理没有自然联系时必须 SKIP", provider.requests[0]["prompt"])
        self.assertEqual(
            recent_daytime_memory_keys(updated, cooldown_hours=72),
            {"memos/day-1"},
        )

        await self.controller.terminate()
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        reloaded = await self.controller.store.read(key)
        self.assertEqual(
            recent_daytime_memory_keys(reloaded, cooldown_hours=72),
            {"memos/day-1"},
        )
        unchanged, sent_again = await self.controller._send_active_message(
            key, reloaded, "daytime_emergence",
        )
        self.assertFalse(sent_again)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(unchanged["revision"], reloaded["revision"])

    async def test_daytime_plain_text_fallback_cools_all_supplied_sources(self):
        await self.controller.terminate()
        provider = FakeProvider(["忽然想起了过去的一点小事。"])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.plugin._vec = FakeVec([
            {
                "memo_name": f"memos/day-{index}",
                "preview": f"第 {index} 段过去记忆。",
                "ts_text": f"2026-07-0{index}",
                "importance": 5,
                "event_ts": index,
            }
            for index in (1, 2)
        ])
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings["daytime_memory_items"] = 2
        key = "character:测试角色"
        state = await self.controller.store.update(
            key,
            lambda current: {**current, "lastUmo": "default:friend"},
        )
        updated, sent = await self.controller._send_active_message(
            key, state, "daytime_emergence",
        )
        self.assertTrue(sent)
        self.assertEqual(
            recent_daytime_memory_keys(updated, cooldown_hours=72),
            {"memos/day-1", "memos/day-2"},
        )

    async def test_concurrent_daytime_due_checks_cannot_double_send(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"memory_index":1,"message":"忽然想起那天窗边没说完的话。"}',
        ])
        context = ProviderContext(provider)
        self.plugin = FakePlugin(context)
        self.plugin._vec = FakeVec([{
            "memo_name": "memos/day-1",
            "preview": "那天窗边还有一句话没说完。",
            "ts_text": "2026-07-01",
            "importance": 5,
            "event_ts": 100,
        }])
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        key = "character:测试角色"
        state = await self.controller.store.update(
            key,
            lambda current: {**current, "lastUmo": "default:friend"},
        )
        results = await asyncio.gather(
            self.controller._send_active_message(key, state, "daytime_emergence"),
            self.controller._send_active_message(key, state, "daytime_emergence"),
        )
        self.assertEqual(sum(1 for _, sent in results if sent), 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(context.sent), 1)

    async def test_dream_receives_body_tendencies_without_cycle_labels(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"dream":"雨落在窗边","residue":"身体有些沉","awareness":"想慢一点"}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        self.controller.settings.update({
            "body_rhythm_enable": True,
            "body_anchor_date": datetime.now(timezone.utc).date().isoformat(),
            "body_daily_variation": 0,
            "body_time_modulation": False,
        })
        key = "character:测试角色"
        state = await self.controller.store.read(key)
        updated = await self.controller._create_dream(key, state)
        prompt = provider.requests[0]["prompt"]
        self.assertIn("当前身体底色", prompt)
        self.assertIn("身体处在较低负荷", prompt)
        self.assertNotIn("当前处于经期", prompt)
        self.assertEqual(updated["recentDreams"][-1]["source"], "llm")

    async def test_significant_mode_does_not_feed_neutral_phase_text_into_dream(self):
        await self.controller.terminate()
        provider = FakeProvider([
            '{"dream":"一片安静的走廊","residue":"很平静","awareness":"不必着急"}',
        ])
        self.plugin = FakePlugin(ProviderContext(provider))
        self.controller = XinchaoController(self.plugin, Path(self.temp.name))
        await self.controller.initialize()
        anchor = datetime.now(timezone.utc).date() - timedelta(days=19)
        self.controller.settings.update({
            "body_rhythm_enable": True,
            "body_anchor_date": anchor.isoformat(),
            "body_daily_variation": 0,
            "body_expression_mode": "significant",
            "body_time_modulation": False,
            "time_zone": "UTC",
        })
        key = "character:测试角色"
        state = await self.controller.store.read(key)
        await self.controller._create_dream(key, state)
        prompt = provider.requests[0]["prompt"]
        self.assertIn("当前没有需要进入梦境的显著身体体验", prompt)
        self.assertNotIn("身体总体仍然平稳", prompt)


class MainHookIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_diary_age_and_compression_use_bounded_time_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(
                FakeContext(),
                {
                    "vec_db_path": str(Path(temp) / "memories.db"),
                    "webui_enable": False,
                    "enable_auto_compress": False,
                },
            )
            event_ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
            block = plugin._format_inject_block(
                {
                    "memo_name": "时间测试",
                    "event_ts": event_ts,
                    "occurred_at": "2026-07-01",
                    "ts_text": "2026-07-01",
                    "time_basis": "explicit",
                    "memory_type": "plot_fact",
                    "importance": 3,
                },
                "那天发生了一件需要记住的事。",
                reference_now_ts=event_ts + 2 * 86400,
            )
            self.assertIn("发生于约 2 天前", block)

            now_ts = datetime.now(timezone.utc).timestamp()
            plugin._session_time["session"] = {
                "snapshot_ts": now_ts,
                "solar_date": "2026-08-02",
                "period": "夜晚",
            }
            self.assertEqual(
                plugin._compression_time_context("session", "auto")["solar_date"],
                "2026-08-02",
            )
            self.assertEqual(plugin._compression_time_context("session", "eod"), {})
            plugin._session_time["session"]["snapshot_ts"] = now_ts - 1801
            self.assertEqual(plugin._compression_time_context("session", "auto"), {})

    async def test_empty_index_request_and_response_remain_non_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(
                FakeContext(),
                {
                    "vec_db_path": str(Path(temp) / "memories.db"),
                    "webui_enable": False,
                    "enable_auto_compress": False,
                    "rp_enhancer_enable": False,
                    "context_governance_enable": False,
                    "cache_friendly_system_guard_enable": False,
                    "cache_prefix_drift_enable": False,
                },
            )
            await plugin._xinchao.initialize()
            event = FakeEvent("新 bot 的第一句话")
            request = ProviderRequest(prompt=event.message_str)
            await asyncio.wait_for(plugin.on_llm_request(event, request), timeout=1)
            self.assertEqual(plugin._last_injection_stats[-1]["outcome"], "empty_index")

            response = SimpleNamespace(is_chunk=False, completion_text="我听见了。")
            await asyncio.wait_for(plugin.on_llm_response(event, response), timeout=1)
            await asyncio.sleep(0.05)
            state = await plugin._xinchao.status()
            self.assertIsNotNone(state["lastPerception"])
            await plugin._xinchao.terminate()

    async def test_request_uses_one_time_snapshot_and_leaves_astr_kb_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(
                FakeContext(),
                {
                    "vec_db_path": str(Path(temp) / "memories.db"),
                    "webui_enable": False,
                    "enable_auto_compress": False,
                    "rp_time_timezone": "Asia/Shanghai",
                    "context_governance_enable": False,
                    "cache_prefix_drift_enable": False,
                },
            )
            await plugin._xinchao.initialize()
            plugin._xinchao.settings.update({
                "body_rhythm_enable": True,
                "body_anchor_date": "2026-07-01",
                "body_daily_variation": 0,
                "body_time_modulation": True,
                "time_zone": "Asia/Shanghai",
            })
            fixed_now = datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc)
            plugin._request_now = lambda: fixed_now
            event = FakeEvent("测试时间边界")
            astr_kb = "角色设定\n[Related Knowledge Base Results]:\n必须保留的 Astr 知识库原文"
            request = ProviderRequest(prompt=event.message_str, system_prompt=astr_kb)

            await asyncio.wait_for(plugin.on_llm_request(event, request), timeout=1)

            extra_text = plugin._extra_parts_text(request)
            self.assertIn("本轮唯一当前时间: 2026-08-03 07:59", extra_text)
            self.assertEqual(event.extra["xinchao_body_state"]["date"], "2026-08-03")
            self.assertEqual(event.extra["xinchao_body_state"]["timeBand"], "morning")
            self.assertEqual(request.system_prompt, astr_kb)
            self.assertNotIn("kb_cache", plugin._last_injection_stats[-1]["composition"])
            self.assertFalse(hasattr(plugin, "kb_cache_enable"))
            await plugin._xinchao.terminate()

    async def test_episodic_request_injects_diary_and_query_evidence_as_temp_content(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(
                FakeContext(),
                {
                    "vec_db_path": str(Path(temp) / "memories.db"),
                    "episodic_db_path": str(Path(temp) / "episodic.db"),
                    "webui_enable": False,
                    "enable_auto_compress": False,
                    "rp_enhancer_enable": False,
                    "context_governance_enable": False,
                    "cache_friendly_system_guard_enable": False,
                    "cache_prefix_drift_enable": False,
                    "enable_affiliate_profile": False,
                    "enable_time_insight_affiliate": False,
                    "recall_dedup_window": 0,
                },
            )
            await plugin._xinchao.initialize()
            plugin._vec = VectorStore(str(Path(temp) / "memories.db"), 3, "test-embedding")
            await plugin._vec.init()
            await plugin._vec.insert_chunks(
                "memos/rain", ["雨夜里他害怕我突然消失，我答应离开前会先告诉他。"],
                [[1.0, 0.0, 0.0]], ts_text="2026-08-03", occurred_at="2026-08-03",
                event_ts=1785686400.0, time_basis="explicit", importance=5,
                memory_type="promise_or_rule", scene_anchor="雨夜里不再突然消失的约定",
                retrieval_key="雨夜 突然消失 离开前告诉 约定",
                state_change="从不安变得更愿意相信",
                passages=[{"text": "雨夜里他害怕我突然消失，我答应离开前会先告诉他。", "passage_index": 0, "char_start": 0, "char_end": 25}],
            )
            plugin._episodes = EpisodicStore(str(Path(temp) / "episodic.db"), 3, "test-embedding")
            await plugin._episodes.init()
            batch = plugin._episodes.archive_batch(
                "session",
                [
                    {"role": "user", "content": "我怕你突然消失。"},
                    {"role": "assistant", "content": "离开前我会先告诉你。"},
                ],
                "auto",
            )
            plugin._episodes.upsert_episode(
                memo_name="memos/rain",
                episode={
                    "occurred_at": "2026-08-03", "event_ts": 1785686400.0,
                    "time_basis": "explicit", "memory_type": "promise_or_rule", "importance": 5,
                    "scene_anchor": "雨夜里不再突然消失的约定",
                    "retrieval_key": "雨夜 突然消失 离开前告诉 约定",
                    "state_change": "从不安变得更愿意相信",
                    "long_effect": "以后离开前会更主动说明",
                    "trigger_hint": "谈到失联时先确认他的不安",
                    "unresolved": ["仍害怕承诺落空"],
                    "evidence": [
                        {"actor": "user", "detail": "他害怕我突然消失", "quote": "我怕你突然消失。", "turn_indexes": [0], "confidence": 0.96, "grounded": True},
                        {"actor": "assistant", "detail": "我答应离开前先告诉他", "quote": "离开前我会先告诉你。", "turn_indexes": [1], "confidence": 0.98, "grounded": True},
                    ],
                },
                card_text="雨夜 突然消失 离开前告诉 约定 从不安到相信",
                embedding=[1.0, 0.0, 0.0], source_batch_id=batch,
                source_kind="auto", legacy=False, evidence_quality="source_grounded",
            )
            plugin._episode_migration_ready = True
            plugin._episode_migration_state = {"status": "ready"}
            plugin._initialized = True

            class Memos:
                async def get_memo(self, _memo_name):
                    return {"content": "2026-08-03\n雨夜里他害怕我突然消失，我答应离开前会先告诉他。"}

            plugin._memos = Memos()
            embed_calls = []

            async def embed(_text, timeout=None, offload_thread=False):
                embed_calls.append(1)
                return [1.0, 0.0, 0.0]

            plugin._embed = embed
            event = FakeEvent("你还记得雨夜里那个不会突然消失的约定吗")
            request = ProviderRequest(prompt=event.message_str, system_prompt="稳定角色设定")
            try:
                await asyncio.wait_for(plugin.on_llm_request(event, request), timeout=3)
                extra = plugin._extra_parts_text(request)
                self.assertIn("<HistoricalMemory", extra)
                self.assertIn("[事件核心]", extra)
                self.assertIn("[日记视角]", extra)
                self.assertIn("[一手证据]", extra)
                self.assertIn("离开前我会先告诉你", extra)
                self.assertEqual(request.system_prompt, "稳定角色设定")
                self.assertEqual(len(embed_calls), 1)
                stat = plugin._last_injection_stats[-1]
                self.assertEqual(stat["outcome"], "injected")
                self.assertEqual(stat["recall_postprocess"]["lean"]["mode"], "lean_full_memory_fusion")
                self.assertEqual(stat["recall_postprocess"]["lean"]["embedding_queries"], 1)
                self.assertGreater(stat["composition"]["diary"], 0)
                self.assertGreater(stat["composition"]["event_core"], 0)
                self.assertGreater(stat["composition"]["evidence"], 0)
            finally:
                await plugin._xinchao.terminate()
                plugin._episodes.close()
                plugin._vec.close()


class WebUIRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_xinchao_page_and_settings_api_are_live(self):
        with tempfile.TemporaryDirectory() as temp:
            chat_provider = FakeProvider(
                provider_id="chat_main",
                model="deepseek-v4-pro",
            )
            protected_values = {
                "character_name": "手填角色名",
                "memos_base_url": "http://127.0.0.1:18082",
                "memos_mode": "external",
                "memos_token": "manual-token",
                "managed_memos_dir": str(Path(temp) / "managed-root"),
                "managed_memos_exe": str(Path(temp) / "managed-root" / "memos.exe"),
                "managed_memos_data_dir": str(Path(temp) / "managed-data"),
                "managed_memos_host": "127.0.0.2",
                "managed_memos_port": 18083,
                "managed_memos_port_start": 18100,
                "vec_db_path": str(Path(temp) / "memories.db"),
                "episodic_db_path": str(Path(temp) / "episodic.db"),
                "data_backup_dir": str(Path(temp) / "data_backups"),
                "context_archive_backup_dir": str(Path(temp) / "context_backups"),
                "webui_host": "127.0.0.1",
                "webui_port": 0,
                "emb_provider_id": "emb_manual",
                "compress_provider_id": "chat_main",
                "query_plan_llm_provider_id": "planner_manual",
                "episode_extraction_provider_id": "episode_manual",
                "diary_render_provider_id": "diary_manual",
                "semantic_state_provider_id": "state_manual",
                "time_insight_llm_provider_id": "insight_manual",
                "profile_provider_id": "profile_manual",
                "rerank_provider_id": "rerank_manual",
                "rp_time_timezone": "Asia/Shanghai",
                "imp_tier5_keywords": "手填五级词",
                "imp_tier4_keywords": "手填四级词",
                "imp_tier3_keywords": "手填三级词",
                "imp_low_keywords": "手填低级词",
            }
            self.assertEqual(
                _safe_preset_values({**protected_values, "lean_recall_candidate_k": 64}),
                {"lean_recall_candidate_k": 64},
            )
            plugin = MemosMemoryPlugin(
                ProviderContext(chat_provider),
                SavableConfig({
                    **protected_values,
                    "webui_enable": True,
                    "enable_auto_compress": False,
                }),
            )
            await plugin._xinchao.initialize()
            plugin._vec = VectorStore(str(Path(temp) / "memories.db"), 3, "test-embedding")
            await plugin._vec.init()
            plugin._episodes = EpisodicStore(str(Path(temp) / "episodic.db"), 3, "test-embedding")
            await plugin._episodes.init()
            plugin._episodes.upsert_episode(
                memo_name="memos/calendar-test",
                episode={
                    "occurred_at": "2026-07-18", "event_ts": 1784304000.0,
                    "time_basis": "explicit", "memory_type": "promise_or_rule", "importance": 5,
                    "scene_anchor": "七月雨夜的约定", "retrieval_key": "七月 雨夜 约定",
                    "state_change": "关系更信任", "long_effect": "会认真对待这份约定",
                    "trigger_hint": "再次谈到雨夜时自然记起",
                    "evidence": [{"detail": "七月雨夜约定", "grounded": False}],
                },
                card_text="七月雨夜约定",
                embedding=[1.0, 0.0, 0.0],
                evidence_quality="diary_derived",
            )
            plugin._episode_migration_ready = True
            plugin._episode_migration_state = {"status": "ready"}
            plugin._initialized = True
            await plugin._vec.insert_chunks(
                "memos/calendar-test", ["七月的雨夜约定。"], [[1.0, 0.0, 0.0]],
                ts_text="2026-07-18", occurred_at="2026-07-18", event_ts=1784304000.0,
                time_basis="explicit", tags=["雨夜", "约定"], retrieval_key="雨夜约定",
                entities=["爱莉"],
            )
            async def fake_embed(_text):
                return [1.0, 0.0, 0.0]
            plugin._embed = fake_embed
            plugin._last_injection_stats.append({
                "request_id": "real-1",
                "query": "还记得七月的雨夜吗",
                "memos": ["memos/calendar-test"],
                "ts": 1784304000.0,
                "ts_iso": "2026-07-18 20:00:00",
                "outcome": "injected",
                "composition": {"context": 100, "kb_cache": 999},
                "total_est_chars": 1099,
            })
            plugin._log_events.append({
                "category": "kb_cache",
                "message": "legacy event",
                "detail": {},
            })
            server = WebUIServer(plugin)
            self.assertTrue(await server.start())
            port = server._server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                html = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(base + "/xinchao", timeout=3).read().decode("utf-8"),
                )
                self.assertIn("即时评估与后台结算", html)
                self.assertIn("身体节律", html)
                self.assertIn("body_anchor_date", html)
                self.assertIn("body_expression_mode", html)
                self.assertIn("daytime_memory_cooldown_hours", html)
                self.assertIn("跟随当前会话模型", html)
                self.assertIn("live_perception_provider_id", html)
                self.assertIn("post_perception_timeout_seconds", html)
                self.assertIn("post_perception_coalesce_enable", html)
                self.assertIn("post_perception_max_batch_turns", html)
                self.assertIn('data-tab="time-insight"', html)
                self.assertIn("time_insight_repeat_cooldown_minutes", html)
                overview = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/xinchao/overview", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(overview["ok"])
                self.assertEqual(overview["data"]["version"], "4.6.4")
                self.assertIn("providerOptions", overview["data"])
                self.assertEqual(overview["data"]["providerOptions"][0]["id"], "chat_main")
                insight = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/time-insight/settings", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(insight["ok"])
                self.assertIn("time_insight_query_min_score", insight["data"]["settings"])
                self.assertEqual(insight["data"]["providerOptions"][0]["id"], "chat_main")
                save_payload = json.dumps({
                    "settings": {"time_insight_repeat_cooldown_minutes": 240}
                }).encode("utf-8")
                save_request = urllib.request.Request(
                    base + "/api/time-insight/settings/save",
                    data=save_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                saved = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(save_request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(saved["ok"])
                self.assertEqual(saved["data"]["settings"]["time_insight_repeat_cooldown_minutes"], 240)

                dashboard = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8"),
                )
                console = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(base + "/console", timeout=3).read().decode("utf-8"),
                )
                self.assertNotIn("KB 分析", dashboard)
                self.assertNotIn("kb_cache", dashboard)
                self.assertIn("时间模型", dashboard)
                self.assertIn("prefers-reduced-motion", dashboard)
                self.assertIn("scroll-snap-type", dashboard)
                self.assertIn("toggleProfileHistory", dashboard)
                self.assertIn("profile-version-toggle", dashboard)
                self.assertIn("aria-expanded='false'", dashboard)
                self.assertIn('data-tab="context"', dashboard)
                self.assertIn('data-tab="settings"', dashboard)
                self.assertIn('data-tab="debug"', dashboard)
                self.assertIn('data-tab="backups"', dashboard)
                self.assertIn("备份管理", dashboard)
                self.assertIn("下次重载恢复", dashboard)
                self.assertIn('data-tab="timeline"', dashboard)
                self.assertIn('data-tab="episodic"', dashboard)
                self.assertIn("情景证据", dashboard)
                self.assertIn("月份档案", dashboard)
                self.assertIn("month-calendar", dashboard)
                self.assertIn("4.x 三层记忆状态", dashboard)
                self.assertIn("arch-source-value", dashboard)
                self.assertIn("4.4 请求路径", dashboard)
                self.assertIn("记忆结构", dashboard)
                self.assertIn("证据来源", dashboard)
                self.assertIn("原文回链", dashboard)
                self.assertNotIn("重要性分布", dashboard)
                self.assertNotIn("记忆生产工作台", dashboard)
                self.assertEqual(dashboard.count('href="/production"'), 1)
                self.assertEqual(dashboard.count('id="backup-meta"'), 1)
                self.assertEqual(dashboard.count('id="context-backup-meta"'), 1)
                self.assertNotIn('data-tab="search"', dashboard)
                self.assertNotIn('data-tab="graph"', dashboard)
                self.assertNotIn('id="month-route-q"', dashboard)
                self.assertIn("真实注入记录", dashboard)
                self.assertIn("召回数值预设", dashboard)
                self.assertIn("不漏检安全网观测", dashboard)
                self.assertIn("从真实记忆生成", dashboard)
                self.assertNotIn('data-tab="summary"', dashboard)
                self.assertNotIn('data-tab="anchors"', dashboard)
                self.assertNotIn("KB Cache", console)
                self.assertNotIn("kb_cache", console)
                self.assertIn("Provider Cache", console)
                status = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/status", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(status["data"]["time_model"]["request_snapshot"])
                self.assertEqual(status["data"]["time_model"]["current_timezone"], "Asia/Shanghai")
                self.assertEqual(status["data"]["recall_architecture"]["active"], "lean_full_memory_fusion")
                self.assertFalse(status["data"]["recall_architecture"]["multi_query"])
                self.assertFalse(status["data"]["recall_architecture"]["month_route"])
                self.assertEqual(status["data"]["recall_architecture"]["story_min"], 1)
                # 4.5: default story ceiling raised 4 -> 6; budgeted injection
                # governs actual volume instead of a tight count cap.
                self.assertEqual(status["data"]["recall_architecture"]["story_max"], 6)
                self.assertTrue(status["data"]["eod_checkpoint"]["enabled"])
                self.assertEqual(status["data"]["eod_checkpoint"]["schedule"], "23:45")
                self.assertEqual(status["data"]["eod_checkpoint"]["min_turns"], 1)
                self.assertEqual(status["data"]["eod_checkpoint"]["max_diaries"], 6)
                self.assertTrue(status["data"]["data_backup"]["enabled"])
                self.assertEqual(status["data"]["data_backup"]["interval_days"], 14)
                plugin_settings = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/settings", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertIn("原文档案与日记生成", plugin_settings["data"]["groups"])
                self.assertIn("剧情注入与原文证据", plugin_settings["data"]["groups"])
                self.assertIn("兼容回退（不参与当前主路）", plugin_settings["data"]["groups"])
                self.assertIn("运行与维护", plugin_settings["data"]["groups"])
                setting_items = {item["key"]: item for item in plugin_settings["data"]["items"]}
                llm_provider_keys = {
                    "compress_provider_id",
                    "query_plan_llm_provider_id",
                    "episode_extraction_provider_id",
                    "diary_render_provider_id",
                    "semantic_state_provider_id",
                    "profile_provider_id",
                }
                for provider_key in llm_provider_keys:
                    options = setting_items[provider_key]["options"]
                    option_values = {item["value"] for item in options}
                    self.assertEqual(options[0]["value"], "")
                    self.assertIn("chat_main", option_values)
                    self.assertIn(
                        setting_items[provider_key]["value"],
                        option_values,
                    )
                self.assertEqual(setting_items["emb_provider_id"]["options"], [])
                self.assertEqual(setting_items["rerank_provider_id"]["options"], [])
                self.assertEqual(setting_items["emb_provider_id"]["value"], "emb_manual")
                self.assertEqual(setting_items["rerank_provider_id"]["value"], "rerank_manual")
                self.assertTrue(setting_items["lean_event_index_enable"]["value"])
                self.assertTrue(setting_items["lean_source_evidence_enable"]["value"])
                self.assertTrue(setting_items["lean_coverage_selection_enable"]["value"])
                self.assertTrue(setting_items["lean_adaptive_evidence_enable"]["value"])
                self.assertTrue(setting_items["passage_vector_auto_migrate"]["value"])
                self.assertTrue(setting_items["source_turn_vector_auto_migrate"]["value"])
                self.assertTrue(setting_items["recall_cross_layer_consistency_enable"]["value"])
                self.assertTrue(setting_items["recall_temporal_constraints_enable"]["value"])
                self.assertTrue(setting_items["recall_intent_layer_weights_enable"]["value"])
                self.assertTrue(setting_items["recall_observation_enable"]["value"])
                item_keys = {item["key"] for item in plugin_settings["data"]["items"]}
                self.assertIn("eod_checkpoint_enable", item_keys)
                self.assertIn("eod_checkpoint_max_diaries", item_keys)
                self.assertIn("data_backup_enable", item_keys)
                self.assertIn("data_backup_interval_days", item_keys)
                self.assertIn("data_backup_keep", item_keys)
                self.assertIn("data_backup_dir", item_keys)
                for preset in plugin_settings["data"]["presets"]:
                    self.assertTrue(preset["values"]["evidence_first_generation_enable"])
                    self.assertTrue(preset["values"]["lean_source_evidence_enable"])
                    self.assertTrue(preset["values"]["enable_time_insight_affiliate"])
                    self.assertTrue(preset["values"]["data_backup_enable"])
                    self.assertNotIn("recall_multi_query_enable", preset["values"])
                    self.assertNotIn("recall_month_route_enable", preset["values"])
                    self.assertNotIn("recall_information_gain_enable", preset["values"])
                    for protected_key in protected_values:
                        self.assertNotIn(protected_key, preset["values"])
                    self.assertFalse(any(
                        _is_preset_protected_setting(key)
                        for key in preset["values"]
                    ))
                apply_global_request = urllib.request.Request(
                    base + "/api/settings/preset",
                    data=json.dumps({"preset": "balanced"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                applied_global = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(apply_global_request, timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(applied_global["ok"])
                self.assertEqual(plugin.compress_every_n_turns, 30)
                self.assertTrue(plugin.evidence_first_generation_enable)
                self.assertTrue(plugin.enable_time_insight_affiliate)
                self.assertEqual(plugin.data_backup_interval_days, 14)
                for protected_key, expected in protected_values.items():
                    self.assertEqual(plugin.config.get(protected_key), expected)
                episodic = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/episodic/status", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(episodic["data"]["episodes"], 1)
                episode_list = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/episodic/memories", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(episode_list["data"]["items"][0]["memo_name"], "memos/calendar-test")
                episode_detail = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(
                            base + "/api/episodic/detail/" + urllib.parse.quote("memos/calendar-test", safe=""),
                            timeout=3,
                        ).read().decode("utf-8")
                    ),
                )
                self.assertEqual(episode_detail["data"]["evidence_quality"], "diary_derived")
                state_status = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/state/status", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(state_status["ok"])
                self.assertTrue(state_status["data"]["enabled"])
                self.assertFalse(state_status["data"]["ready"])
                case_payload = json.dumps({
                    "query": "还记得七月的雨夜吗",
                    "expected_memos": ["memos/calendar-test"],
                    "source": "test",
                }).encode("utf-8")
                case_request = urllib.request.Request(
                    base + "/api/eval/case", data=case_payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                case_saved = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(case_request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(case_saved["ok"])
                case_id = case_saved["data"]["case_id"]
                eval_cases = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/eval/cases", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(eval_cases["data"]["items"][0]["expected_memos"], ["memos/calendar-test"])
                recall_presets = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/eval/presets", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(len(recall_presets["data"]["items"]), 4)
                self.assertEqual(recall_presets["data"]["items"][0]["id"], "recall_effect")
                recall_by_id = {
                    item["id"]: item for item in recall_presets["data"]["items"]
                }
                effect_values = recall_by_id["recall_effect"]["values"]
                narrative_values = recall_by_id["recall_narrative"]["values"]
                self.assertEqual(effect_values["lean_story_max_inject"], 10)
                self.assertEqual(narrative_values["lean_story_max_inject"], 10)
                self.assertGreater(
                    narrative_values["lean_recall_candidate_k"],
                    effect_values["lean_recall_candidate_k"],
                )
                self.assertGreater(
                    narrative_values["lean_relative_margin_broad"],
                    effect_values["lean_relative_margin_broad"],
                )
                self.assertTrue(effect_values["lean_source_evidence_enable"])
                self.assertTrue(effect_values["recall_intent_layer_weights_enable"])
                apply_recall_request = urllib.request.Request(
                    base + "/api/eval/preset",
                    data=json.dumps({"preset": "recall_effect"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                applied_recall = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(apply_recall_request, timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(applied_recall["ok"])
                self.assertEqual(plugin.lean_recall_candidate_k, 80)
                self.assertEqual(plugin.lean_story_normal_inject, 4)
                self.assertEqual(plugin.lean_story_max_inject, 10)
                for protected_key, expected in protected_values.items():
                    self.assertEqual(plugin.config.get(protected_key), expected)

                auto_request = urllib.request.Request(
                    base + "/api/eval/auto_cases",
                    data=json.dumps({"limit": 20}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                auto_cases = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(auto_request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(auto_cases["ok"])
                self.assertFalse(auto_cases["data"]["memory_content_changed"])
                self.assertGreaterEqual(auto_cases["data"]["generated"], 1)

                plugin._episodes.record_recall_observation({
                    "request_id": "web-obs", "query": "雨夜约定", "intent": "specific_event",
                    "safety_triggered": True, "selected_before": [],
                    "selected_after": ["memos/calendar-test"],
                    "rescue_selected": ["memos/calendar-test"], "outcome": "helped",
                })
                observations = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/eval/observations", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(observations["data"]["summary"]["helped"], 1)
                observation_feedback = urllib.request.Request(
                    base + "/api/eval/observation_feedback",
                    data=json.dumps({"request_id": "web-obs", "feedback": "useful"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                observed = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(observation_feedback, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(observed["data"]["updated"])
                self.assertEqual(observed["data"]["linked_memories"], 1)
                linked_events = plugin._vec.feedback_event_list(limit=10)
                self.assertEqual(linked_events[0]["memo_name"], "memos/calendar-test")
                self.assertEqual(linked_events[0]["action"], "useful")
                self.assertEqual(linked_events[0]["source"], "safety_observation")
                delete_request = urllib.request.Request(
                    base + "/api/eval/case/" + urllib.parse.quote(case_id, safe=""), method="DELETE",
                )
                case_deleted = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(delete_request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(case_deleted["data"]["deleted"])
                stats = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/stats", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(stats["data"]["by_month"], [{"k": "2026-07", "v": 1}])
                self.assertEqual(
                    stats["data"]["memory_structure"]["memory_types"],
                    [{"k": "plot_fact", "v": 1}],
                )
                self.assertEqual(
                    stats["data"]["memory_structure"]["evidence_quality"][2],
                    {"k": "diary_derived", "v": 1},
                )
                backup_request = urllib.request.Request(
                    base + "/api/data-backup/run",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                backup_result = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(backup_request, timeout=5).read().decode("utf-8")
                    ),
                )
                self.assertTrue(backup_result["data"]["created"])
                backup_file = backup_result["data"]["file"]
                self.assertTrue((Path(temp) / "data_backups" / backup_file).is_file())
                backup_list = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/data-backup/list", timeout=5).read().decode("utf-8")
                    ),
                )
                self.assertEqual(backup_list["data"]["items"][0]["file"], backup_file)
                backup_inspect = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(
                            base + "/api/data-backup/inspect?file=" + urllib.parse.quote(backup_file),
                            timeout=5,
                        ).read().decode("utf-8")
                    ),
                )
                self.assertTrue(backup_inspect["data"]["valid"])
                downloaded = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(
                        base + "/api/data-backup/download?file=" + urllib.parse.quote(backup_file),
                        timeout=5,
                    ).read(),
                )
                self.assertTrue(downloaded.startswith(b"PK"))
                restore_request = urllib.request.Request(
                    base + "/api/data-backup/restore",
                    data=json.dumps({
                        "file": backup_file, "confirm": "RESTORE_ON_RELOAD",
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                restore_result = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(restore_request, timeout=10).read().decode("utf-8")
                    ),
                )
                self.assertTrue(restore_result["data"]["scheduled"])
                self.assertTrue(restore_result["data"]["pending_restore"]["safety_backup"])
                cancel_request = urllib.request.Request(
                    base + "/api/data-backup/cancel-restore",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                )
                cancel_result = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(cancel_request, timeout=5).read().decode("utf-8")
                    ),
                )
                self.assertTrue(cancel_result["data"]["cancelled"])
                console_stats = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/console/stats", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertNotIn("kb_cache", console_stats["data"]["latest"]["composition"])
                self.assertEqual(console_stats["data"]["latest"]["total_est_chars"], 100)
                timeline = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/timeline?month=2026-07", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(timeline["ok"])
                self.assertEqual(timeline["data"]["selected"], "2026-07")
                self.assertEqual(timeline["data"]["view"]["mode"], "archive_only")
                self.assertFalse(timeline["data"]["view"]["participates_in_recall"])
                self.assertEqual(timeline["data"]["calendar"]["memo_count"], 1)
                month_route = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/month-route?q=" + urllib.parse.quote("雨夜约定"), timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(month_route["data"]["months"][0]["year_month"], "2026-07")
                feedback_payload = json.dumps({
                    "request_id": "real-1", "memo_name": "memos/calendar-test",
                    "query": "还记得七月的雨夜吗", "action": "useful", "source": "test",
                }).encode("utf-8")
                feedback_request = urllib.request.Request(
                    base + "/api/feedback/apply", data=feedback_payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                feedback_saved = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(feedback_request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(feedback_saved["ok"])
                feedback_list = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/feedback", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertEqual(feedback_list["data"]["feedback"][0]["action"], "useful")
                self.assertFalse(feedback_list["data"]["legacy_active"])
                feedback_id = feedback_list["data"]["feedback"][0]["id"]
                delete_request = urllib.request.Request(
                    base + f"/api/feedback/{feedback_id}", method="DELETE",
                )
                deleted = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(delete_request, timeout=3).read().decode("utf-8")),
                )
                self.assertEqual(deleted["data"]["deleted"], 1)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    await asyncio.to_thread(
                        lambda: urllib.request.urlopen(base + "/api/kb-cache/analysis", timeout=3),
                    )
                self.assertEqual(missing.exception.code, 404)

                settings = dict(overview["data"]["settings"])
                settings["daytime_emergence_enable"] = True
                settings["daytime_memory_cooldown_hours"] = 96
                settings["body_anchor_date"] = "2026-07-01"
                settings["body_expression_mode"] = "immersive"
                payload = json.dumps({"settings": settings}).encode("utf-8")
                request = urllib.request.Request(
                    base + "/api/xinchao/settings/save",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                saved = await asyncio.to_thread(
                    lambda: json.loads(urllib.request.urlopen(request, timeout=3).read().decode("utf-8")),
                )
                self.assertTrue(saved["ok"])
                self.assertTrue(saved["data"]["settings"]["daytime_emergence_enable"])
                self.assertEqual(saved["data"]["settings"]["daytime_memory_cooldown_hours"], 96)
                self.assertEqual(saved["data"]["settings"]["body_anchor_date"], "2026-07-01")
                self.assertEqual(saved["data"]["settings"]["body_expression_mode"], "immersive")
                state = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/xinchao/state", timeout=3).read().decode("utf-8")
                    ),
                )
                self.assertTrue(state["data"]["bodyState"]["available"])
                self.assertIn("live", state["data"]["perceptionHealthByStage"])
                self.assertIn("post", state["data"]["perceptionHealthByStage"])
            finally:
                await server.stop()
                await plugin._xinchao.terminate()
                plugin._episodes.close()
                plugin._vec.close()


if __name__ == "__main__":
    unittest.main()
