from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from astrbot_plugin_memos_memory.data_backup import DataBackupManager
from astrbot_plugin_memos_memory.house_service import (
    HouseService,
    _CHARACTER_IDLE_ANIMATIONS,
    _CHARACTER_PLACEMENTS,
    _CHARACTER_REACTION_ANIMATIONS,
    _CHARACTER_VARIANTS,
    validate_house_settings,
)
from astrbot_plugin_memos_memory.house_motion import (
    MOTION_PROFILES,
    fatigue_band,
    motion_catalog,
    select_reaction,
    select_scene,
    validate_motion_catalog,
)
from astrbot_plugin_memos_memory.house_store import HouseStore
from astrbot_plugin_memos_memory.xinchao_engine import new_state
from astrbot_plugin_memos_memory.xinchao_store import StateStore


class FakeContext:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo: str, chain: object) -> None:
        self.sent.append((umo, chain))


class FakeXinchao:
    def __init__(self, root: Path) -> None:
        self.store = StateStore(root / "xinchao_state.json")
        self.applied: list[tuple[str, dict]] = []

    @staticmethod
    def scope_key_from_query(value: str) -> str:
        return str(value or "scope-a")

    @staticmethod
    def _body_state(**_kwargs):
        return {
            "available": True,
            "phase": "steady",
            "timeBand": "evening",
            "tendencies": ["略有疲惫"],
        }

    async def apply_offline_afterglow(self, key: str, digest: dict) -> dict:
        self.applied.append((key, digest))
        return {"applied": True, "session_id": digest["session_id"]}


class FakeVectorStore:
    def list_memories(self, limit: int = 12):
        return [
            {
                "memo_name": "memos/a",
                "occurred_at": "2026-08-01",
                "memory_type": "relationship",
                "preview": "在雨夜谈起旧约定",
                "long_effect": "更愿意信任彼此",
            },
            {
                "memo_name": "memos/b",
                "occurred_at": "2026-08-02",
                "memory_type": "daily",
                "preview": "一起喝茶",
                "long_effect": "记得清淡的茶香",
            },
        ][:limit]


class FakePlugin:
    def __init__(self, root: Path) -> None:
        self.character_name = "测试角色"
        self.context = FakeContext()
        self._xinchao = FakeXinchao(root)
        self._vec = FakeVectorStore()
        self._episodes = None

    @staticmethod
    def _semantic_state_status():
        return {"state": {"version": 3, "rendered_text": "关系稳定，仍有未说完的话。"}}

    @staticmethod
    def _affiliate_profile_status():
        return {"connected": True, "profile": "克制而细腻", "profile_facts": "喜欢清茶"}


def _make_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES('ok')")
        conn.commit()
    finally:
        conn.close()


class HouseStoreTests(unittest.TestCase):
    def test_linked_session_is_terminal_and_next_cycle_is_new(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HouseStore(Path(temp) / "house.sqlite3")
            store.initialize()
            first, created = store.ensure_session(
                "scope-a", "umo-a", "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00", "Asia/Shanghai", "r1", {},
            )
            self.assertTrue(created)
            store.update_session(first["id"], status="linked", closed_at="2026-08-02T00:00:00+00:00")
            self.assertIsNone(store.get_active_session("scope-a"))
            second, created = store.ensure_session(
                "scope-a", "umo-a", "2026-08-03T00:00:00+00:00",
                "2026-08-03T00:00:00+00:00", "Asia/Shanghai", "r2", {},
            )
            self.assertTrue(created)
            self.assertNotEqual(first["id"], second["id"])

    def test_character_interaction_history_is_newest_first(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HouseStore(Path(temp) / "house.sqlite3")
            store.initialize()
            first = store.record_character_interaction(scope_key="scope-a", reaction_key="day:fan:tap:nod")
            second = store.record_character_interaction(scope_key="scope-a", reaction_key="day:fan:tap:turn")
            rows = store.recent_character_interactions("scope-a", 10)
            self.assertEqual({row["id"] for row in rows}, {first["id"], second["id"]})
            self.assertGreaterEqual(rows[0]["created_at"], rows[1]["created_at"])


class HouseMotionTests(unittest.TestCase):
    def test_catalog_has_95_valid_semantic_profiles(self):
        self.assertEqual(validate_motion_catalog(), [])
        self.assertEqual(set(MOTION_PROFILES), set(_CHARACTER_VARIANTS))
        self.assertEqual(sum(len(items) for items in MOTION_PROFILES.values()), 95)
        all_ids = [item["id"] for items in MOTION_PROFILES.values() for item in items]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertTrue(all(item["label"] for items in MOTION_PROFILES.values() for item in items))

    def test_scene_and_motion_ranking_are_deterministic_and_state_aware(self):
        now = datetime(2026, 9, 7, 19, 15, tzinfo=timezone.utc)
        drives = [{"key": "share", "value": .91}, {"key": "social", "value": .78}]
        first = select_scene(
            scope_key="scope-a", now=now, variants=_CHARACTER_VARIANTS,
            top_drive_rows=drives, fatigue=.12, consciousness="awake", newest_type="letter",
            artifact_drive_rows=[{"key": "share", "confidence": .88}],
        )
        second = select_scene(
            scope_key="scope-a", now=now, variants=_CHARACTER_VARIANTS,
            top_drive_rows=drives, fatigue=.12, consciousness="awake", newest_type="letter",
            artifact_drive_rows=[{"key": "share", "confidence": .88}],
        )
        self.assertEqual(first, second)
        self.assertEqual(first["period"], "dusk")
        self.assertEqual(first["fatigue_band"], "mid")
        self.assertEqual(first["artifact_drives"], [{"key": "share", "confidence": .88}])
        self.assertEqual(set(first["components"]), {"period", "fatigue", "drive", "artifact", "novelty"})
        profiles = motion_catalog(
            first["sprite"], period=first["period"], fatigue=first["fatigue_band"],
            newest_type="letter", top_drive_rows=drives,
        )
        self.assertEqual(len(profiles), 5)
        self.assertGreaterEqual(profiles[0]["weight"], profiles[-1]["weight"])
        self.assertEqual(fatigue_band(.22), "high")

    def test_click_policy_throttles_rapid_and_repeated_interactions(self):
        now = datetime.now(timezone.utc)
        fresh = select_reaction("playful", [], now=now, seed="fresh")
        self.assertNotEqual(fresh["animation"], "still")
        recent = [{"created_at": (now - timedelta(seconds=3)).isoformat(), "reaction_key": "x:nod"}]
        rapid = select_reaction("playful", recent, now=now, seed="rapid")
        self.assertEqual(rapid["animation"], "still")
        self.assertEqual(rapid["tier"], "hot")
        busy = [
            {"created_at": (now - timedelta(seconds=i * 5)).isoformat(), "reaction_key": f"x:{i}"}
            for i in range(5)
        ]
        quiet = select_reaction("gentle", busy, now=now, seed="busy")
        self.assertEqual(quiet["reason"], "five_click_quiet_period")
        self.assertEqual(quiet["cooldown_seconds"], 30)


class HouseServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = FakePlugin(self.root)
        self.service = HouseService(self.plugin, self.root)
        self.service.store.initialize()
        await self.plugin._xinchao.store.replace("scope-a", {
            **new_state(),
            "lastConversationAt": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
            "lastUmo": "umo-a",
        })

    async def asyncTearDown(self) -> None:
        await self.service.terminate()
        self.temp.cleanup()

    def _configure(self) -> None:
        self.service.save_settings({
            "house_enable": True,
            "house_api_base_url": "http://127.0.0.1:9999/v1",
            "house_model": "house-model",
            "house_api_key": "secret-house-key",
        })

    async def _session(self) -> dict:
        state = await self.plugin._xinchao.store.read("scope-a")
        last_at = state["lastConversationAt"]
        session, _ = self.service.store.ensure_session(
            "scope-a", "umo-a", last_at, last_at, "Asia/Shanghai", "rev", {},
        )
        return session

    async def test_disabled_and_unconfigured_generation_do_not_create_content(self):
        disabled = await self.service.generate("scope-a")
        self.assertEqual(disabled["reason"], "disabled")
        self.service.settings["house_enable"] = True
        unconfigured = await self.service.generate("scope-a")
        self.assertEqual(unconfigured["reason"], "incomplete_configuration")
        self.assertEqual(self.service.store.stats("scope-a")["artifacts"], 0)

    async def test_secret_is_masked_and_not_returned(self):
        self._configure()
        payload = self.service.settings_payload()
        self.assertTrue(payload["has_api_key"])
        self.assertEqual(payload["api_key_mask"], "********")
        self.assertNotIn("secret-house-key", json.dumps(payload, ensure_ascii=False))
        with self.assertRaises(ValueError):
            self.service.save_settings({"house_api_base_url": "file:///tmp/key"})

    async def test_generation_keeps_internal_reality_and_rejects_late_result(self):
        self._configure()
        session = await self._session()
        snapshot = await self.service._capture_sources("scope-a", await self.plugin._xinchao.store.read("scope-a"), 30)

        async def late_model(_snapshot, _kind=""):
            self.service.store.update_session(session["id"], status="closing")
            return {
                "artifact_type": "letter",
                "title": "未寄信",
                "content": "我在安静里想起那场雨，但它只是内心的回声。",
                "reality_status": "fact",
                "source_ids": ["memo:memos/a", "invented"],
                "thought_cues": ["想把话说清楚"],
                "drive_candidates": [{"key": "social", "confidence": 0.8}],
                "sendable": True,
            }

        self.service._call_model = late_model
        result = await self.service.generate(
            "scope-a", session=session, snapshot=snapshot,
        )
        self.assertEqual(result["reason"], "late_result")
        self.assertEqual(self.service.store.stats("scope-a")["artifacts"], 0)

    async def test_memory_source_cooldown_filters_recently_used_source(self):
        self._configure()
        session = await self._session()
        self.service.store.insert_artifact({
            "session_id": session["id"], "scope_key": "scope-a",
            "artifact_type": "whisper", "title": "旧念", "content": "旧念仍在。",
            "summary": "旧念", "reality_status": "internal",
            "source_ids": ["memo:memos/a"], "mood": [], "thought_cues": [],
            "drive_candidates": [], "content_hash": "h", "status": "sealed",
        })
        snapshot = await self.service._capture_sources(
            "scope-a", await self.plugin._xinchao.store.read("scope-a"), 30,
        )
        ids = {item["id"] for item in snapshot["memory_evidence"]}
        self.assertNotIn("memo:memos/a", ids)
        self.assertIn("memo:memos/b", ids)

    async def test_wake_freezes_cycle_links_once_and_opens_next_cycle(self):
        self._configure()
        session = await self._session()
        self.service.store.insert_artifact({
            "session_id": session["id"], "scope_key": "scope-a",
            "artifact_type": "whisper", "title": "檐下", "content": "我仍想把话说完。",
            "summary": "仍想说完", "reality_status": "internal",
            "source_ids": ["memo:memos/b"], "mood": ["克制"],
            "thought_cues": ["想把话说完"],
            "drive_candidates": [{"key": "social", "direction": "activate", "confidence": 0.7}],
            "content_hash": "h2", "status": "sealed",
        })
        first = await self.service.before_wake("scope-a", {}, "umo-a")
        second = await self.service.before_wake("scope-a", {}, "umo-a")
        self.assertTrue(first["linked"])
        self.assertEqual(second["reason"], "no_session")
        self.assertEqual(len(self.plugin._xinchao.applied), 1)
        linked = self.service.store.get_session(session["id"])
        self.assertEqual(linked["status"], "linked")
        self.assertIsNone(self.service.store.get_active_session("scope-a"))

    async def test_character_contract_records_pose_expression_and_extension_point(self):
        result = await self.service.character_interact({"scope": "scope-a", "action": "tap"})
        self.assertEqual(result["interaction"]["pose"], result["character"]["pose"])
        self.assertEqual(result["interaction"]["expression"], result["character"]["expression"])
        self.assertEqual(result["extension"]["schema"], 2)
        self.assertTrue(result["extension"]["supports_animation"])
        self.assertIn(result["extension"]["suggested_animation"], {"nod", "startle", "lean", "greet", "soft", "turn"})
        self.assertIn(result["extension"]["cooldown"]["tier"], {"fresh", "ready", "warm"})
        self.assertEqual(result["reaction"]["source"], "local")
        repeated = await self.service.character_interact({"scope": "scope-a", "action": "tap"})
        self.assertEqual(repeated["extension"]["suggested_animation"], "still")
        self.assertEqual(repeated["extension"]["cooldown"]["tier"], "hot")

    async def test_motion_defaults_and_overview_contract_are_stable(self):
        settings = validate_house_settings({
            "house_motion_intensity": "unknown",
            "house_character_render_mode": "unknown",
        })
        self.assertTrue(settings["house_motion_enable"])
        self.assertTrue(settings["house_ambient_enable"])
        self.assertTrue(settings["house_parallax_enable"])
        self.assertEqual(settings["house_motion_intensity"], "balanced")
        self.assertEqual(settings["house_character_render_mode"], "hybrid")
        self.assertEqual(settings["settings_schema_version"], 3)
        overview = await self.service.overview("scope-a")
        self.assertEqual(overview["motion"], {
            "enabled": True,
            "intensity": "balanced",
            "character_render_mode": "hybrid",
            "ambient": True,
            "parallax": True,
        })
        self.assertEqual(overview["environment"]["period_source"], "server_current_time")
        self.assertEqual(overview["environment"]["background_variants"], ["dawn", "day", "dusk", "night"])
        self.assertTrue(overview["environment"]["character_light_match"])

    async def test_character_catalog_exposes_compatible_art_states_and_scene_combinations(self):
        self.assertEqual(len(_CHARACTER_VARIANTS), 19)
        self.assertEqual(len(_CHARACTER_PLACEMENTS), 3)
        self.assertEqual({item["atlas"] for item in _CHARACTER_VARIANTS.values()}, {"a", "b", "c", "d", "e"})
        self.assertEqual(set(_CHARACTER_IDLE_ANIMATIONS), set(_CHARACTER_VARIANTS))
        for sprite, variant in _CHARACTER_VARIANTS.items():
            with self.subTest(sprite=sprite):
                self.assertTrue(variant["location"])
                self.assertTrue(variant["pose"])
                self.assertTrue(variant["expression"])
                self.assertTrue(variant["outfit"])
                self.assertTrue(variant["placement_ids"])
                self.assertTrue(set(variant["placement_ids"]).issubset({"near", "balanced", "far"}))
                if variant["prop_mode"].startswith("fixed_"):
                    self.assertEqual(variant["placement_ids"], ("balanced",))
                self.assertIn(_CHARACTER_IDLE_ANIMATIONS[sprite], {
                    "breathe", "sway", "letter", "tea", "write",
                    "sleepy", "kneel", "playful", "read", "lantern",
                })
                self.assertIn(variant["expression"], _CHARACTER_REACTION_ANIMATIONS)
        scene = self.service._character_scene(
            "scope-a", datetime(2026, 9, 7, 18, tzinfo=timezone.utc),
            {"consciousness": "awake", "fatigue": 0.1}, [], None,
        )
        self.assertEqual(scene["available_variants"], 19)
        self.assertEqual(scene["available_scene_combinations"], 40)
        self.assertEqual(scene["available_motion_profiles"], 95)
        self.assertEqual(scene["meaningful_state_combinations"], 200)
        self.assertEqual(len(scene["motion_profiles"]), 5)
        self.assertLessEqual(scene["motion_scheduler"]["max_duty_cycle"], .25)
        self.assertEqual(scene["selection"]["period_source"], "server_current_time")
        self.assertIn(scene["placement_variant"], scene["compatible_placements"])
        self.assertEqual(scene["rig"]["schema"], 2)
        self.assertEqual(scene["rig"]["profile"], scene["sprite"])
        self.assertEqual(scene["rig"]["renderer"], "webgl_mesh2d")
        self.assertEqual(scene["rig"]["fallback"], "classic_sprite")
        self.assertEqual(len(scene["rig"]["channels"]), 11)


class HouseBackupTests(unittest.TestCase):
    def test_house_database_and_settings_are_backed_up_without_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("memories.db", "episodic_memory.db", "house_state.sqlite3"):
                _make_sqlite(root / name)
            (root / "house_settings.json").write_text(
                json.dumps({"house_enable": True, "house_model": "model"}), encoding="utf-8",
            )
            (root / "house_secret.json").write_text(
                json.dumps({"value": "must-never-enter-backup"}), encoding="utf-8",
            )
            (root / "xinchao_secret.json").write_text(
                json.dumps({"value": "must-never-enter-xinchao-backup"}), encoding="utf-8",
            )
            manager = DataBackupManager(
                backup_dir=str(root / "backups"),
                vec_db_path=str(root / "memories.db"),
                episodic_db_path=str(root / "episodic_memory.db"),
                house_db_path=str(root / "house_state.sqlite3"),
                plugin_version="test8ultra",
            )
            result = manager.create(force=True, reason="house_test")
            self.assertTrue(result["created"])
            with zipfile.ZipFile(result["path"], "r") as archive:
                names = set(archive.namelist())
                self.assertIn("house_state.sqlite3", names)
                self.assertIn("house_settings.json", names)
                self.assertNotIn("house_secret.json", names)
                self.assertNotIn("xinchao_secret.json", names)
                self.assertNotIn(
                    "must-never-enter-backup",
                    "".join(archive.read(name).decode("utf-8", errors="ignore") for name in names),
                )
                self.assertNotIn(
                    "must-never-enter-xinchao-backup",
                    "".join(archive.read(name).decode("utf-8", errors="ignore") for name in names),
                )


class HouseFrontendContractTests(unittest.TestCase):
    def test_default_character_atlas_has_no_detached_alpha_debris(self):
        path = Path(__file__).resolve().parents[1] / "assets/house-character-atlas-a.png"
        image = Image.open(path).convert("RGBA")
        width, height = image.size
        occupied = bytearray(1 if value else 0 for value in image.getchannel("A").tobytes())
        seen = bytearray(width * height)
        component_sizes = []

        for start, filled in enumerate(occupied):
            if not filled or seen[start]:
                continue
            seen[start] = 1
            queue = deque([start])
            component_size = 0
            while queue:
                index = queue.popleft()
                component_size += 1
                x = index % width
                y = index // width
                neighbours = []
                if x:
                    neighbours.append(index - 1)
                if x + 1 < width:
                    neighbours.append(index + 1)
                if y:
                    neighbours.append(index - width)
                if y + 1 < height:
                    neighbours.append(index + width)
                for neighbour in neighbours:
                    if occupied[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        queue.append(neighbour)
            component_sizes.append(component_size)

        component_sizes.sort(reverse=True)
        self.assertEqual(len(component_sizes), 4)
        self.assertGreater(component_sizes[-1], 100_000)

    def test_house_scene_has_real_assets_hotspots_and_character_api(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "house.html").read_text(encoding="utf-8")
        for label in ("门边信箱", "书房案几", "院中茶席", "卧房灯影", "旧木匣"):
            self.assertIn(label, html)
        self.assertIn("/assets/house-courtyard.png", html)
        self.assertIn("/assets/house-courtyard-dawn.png", html)
        self.assertIn("/assets/house-courtyard-dusk.png", html)
        self.assertIn("/assets/house-courtyard-night.png", html)
        self.assertIn("/assets/house-character-atlas-a.png", html)
        self.assertIn("/assets/house-character-atlas-b.png", html)
        self.assertIn(".character{background-color:transparent;width:min(42vw,580px)}", html)
        self.assertIn(".sprite-chest{background-position:100% 0}", html)
        self.assertIn("/api/house/character/interact", html)
        self.assertIn("data-zone=", html)
        self.assertIn('id="ambient"', html)
        self.assertIn('id="character-sprite"', html)
        self.assertIn('id="character-rig"', html)
        self.assertIn('/assets/house-character-rig.js', html)
        self.assertIn('id="s-character-render"', html)
        self.assertIn("new HouseCharacterRig", html)
        self.assertIn("window.__houseRigDebug", html)
        self.assertIn("class MotionGarden", html)
        self.assertIn("class BackgroundDirector", html)
        self.assertIn("localPeriod", html)
        self.assertIn("function previewOverview", html)
        self.assertIn("previewMode", html)
        self.assertIn("const previewScenes=", html)
        self.assertIn("function cyclePreviewScene", html)
        self.assertIn("schedulePreviewTour(4500)", html)
        self.assertIn("motion_scheduler:{check_seconds:[2,5]", html)
        self.assertIn("风吹过来时，衣袖也会跟着动", html)
        self.assertIn("const manualPositions=", html)
        self.assertIn("const characterDetails=", html)
        self.assertIn("window.__houseRigDebugControl", html)
        self.assertIn("function cycleCharacterExpression", html)
        self.assertIn("function cycleCharacterPosition", html)
        self.assertIn("contextmenu", html)
        self.assertIn("doubleWindow=360", html)
        self.assertIn('class="foreground-depth"', html)
        self.assertIn("class CharacterMotionDirector", html)
        self.assertIn("requestAnimationFrame", html)
        self.assertIn("document.hidden", html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn('id="s-motion-intensity"', html)
        self.assertIn('id="s-ambient"', html)
        self.assertIn('id="s-parallax"', html)
        self.assertIn("max_duty_cycle", html)
        self.assertIn("motion-playing", html)
        self.assertIn("interruptForReaction", html)
        self.assertNotIn("<video", html)
        for name in (
            "house-courtyard.png", "house-courtyard-dawn.png",
            "house-courtyard-dusk.png", "house-courtyard-night.png",
            "house-character-atlas-a.png",
            "house-character-atlas-b.png", "house-character-atlas-c.png",
            "house-character-atlas-d.png",
        ):
            path = root / "assets" / name
            self.assertGreater(path.stat().st_size, 10_000)
            if name.startswith("house-character-atlas-"):
                # PNG IHDR colour type 6 is true RGBA, not a baked checkerboard.
                self.assertEqual(path.read_bytes()[25], 6)

        rig_source = (root / "assets" / "house-character-rig.js").read_text(encoding="utf-8")
        self.assertIn("webgl-mesh2d", rig_source)
        self.assertIn("createMesh(gl, 64, 64)", rig_source)
        self.assertIn("torso_expansion", rig_source)
        self.assertIn("uFaceMotion", rig_source)
        self.assertIn("forceBlink", rig_source)
        self.assertIn('case "sip"', rig_source)
        self.assertIn('case "tend"', rig_source)
        self.assertIn("this._pointerMove", rig_source)
        self.assertIn("this.pointer.active", rig_source)
        self.assertIn("prepareAtlas", rig_source)
        self.assertIn("--house-atlas-", rig_source)
        self.assertIn("balanced: 1.42", rig_source)
        self.assertNotIn("setInterval", rig_source)
        expected_profiles = {
            "fan", "letter", "tea", "desk", "window", "chest", "playful", "reading",
            "lantern", "wistful", "flowers", "steps", "yawn", "lean", "book", "cup",
            "court-smile", "court-surprised", "court-soft",
        }
        for profile in expected_profiles:
            token = f'"{profile}":' if "-" in profile else f"{profile}:"
            self.assertIn(token, rig_source)

        html = (root / "house.html").read_text(encoding="utf-8")
        self.assertIn("forceBlink(380)", html)
        self.assertIn("她端起茶盏抿了一口", html)
        self.assertIn("她俯身理好花枝", html)
        self.assertIn("廊阶静坐", html)

    def test_each_character_pose_is_an_isolated_rgba_texture(self):
        root = Path(__file__).resolve().parents[1]
        sprites = (
            "fan", "letter", "tea", "desk", "window", "chest", "playful", "reading",
            "lantern", "wistful", "flowers", "steps", "yawn", "lean", "book", "cup",
            "court-smile", "court-surprised", "court-soft",
        )
        html = (root / "house.html").read_text(encoding="utf-8")
        for sprite in sprites:
            with self.subTest(sprite=sprite):
                path = root / "assets" / f"house-character-{sprite}.png"
                self.assertGreater(path.stat().st_size, 10_000)
                image = Image.open(path).convert("RGBA")
                self.assertEqual(image.size, (704, 704))
                alpha = image.getchannel("A")
                self.assertIsNone(alpha.getbbox() and (
                    alpha.crop((0, 0, 704, 6)).getbbox()
                    or alpha.crop((0, 698, 704, 704)).getbbox()
                    or alpha.crop((0, 0, 6, 704)).getbbox()
                    or alpha.crop((698, 0, 704, 704)).getbbox()
                ))
                self.assertIn(f"/assets/house-character-{sprite}.png", html)

        rig_source = (root / "assets" / "house-character-rig.js").read_text(encoding="utf-8")
        self.assertIn("this.loadTexture(nextSprite)", rig_source)
        self.assertIn('image.src = "/assets/house-character-" + sprite + ".png"', rig_source)
        self.assertIn("this.sprite = nextSprite", rig_source)
        self.assertNotIn("cell[0] * 0.5", rig_source)


if __name__ == "__main__":
    unittest.main()
