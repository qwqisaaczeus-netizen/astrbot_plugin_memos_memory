# -*- coding: utf-8 -*-
"""记忆生产独立页面与 HTTP API 黑盒验收。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.webui import WebUIServer


class ProductionWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.temp.name) / "episodic.db"), 3, "model-a")
        await self.store.init()
        batch = self.store.archive_batch(
            "web-session",
            [{"role": "user", "content": "完整原文与雨夜约定", "event_ts": 100.0,
              "event_timezone": "UTC"}],
            "auto",
        )
        self.store.upsert_episode(
            memo_name="memos/web-one",
            episode={
                "episode_id": "ep-web-one", "scene_start_turn": 0, "scene_end_turn": 0,
                "scene_boundary_reasons": ["relation_turn"],
                "evidence": [{"kind": "commitment", "detail": "雨夜约定",
                              "tier": "must_write", "turn_indexes": [0], "grounded": True}],
            },
            card_text="雨夜约定" * 1000,
            embedding=None,
            source_batch_id=batch,
            source_kind="auto",
            legacy=False,
            # A user-edited Episode is still recoverable when its exact links survive.
            evidence_quality="mixed_user_edited",
            diary_content_hash="web-hash",
            diary_render_version="4.6.2",
            must_coverage=1.0,
            support_coverage=0.75,
            transcript_risk=0.1,
            source_overlap_ratio=0.22,
            direct_quote_ratio=0.08,
            compression_ratio=0.48,
        )
        # Prepare a pending generation so the production action is genuinely live.
        await self.store.ensure_dim(4, "model-b")
        generation = self.store.stats()["pending_generation"]
        rows = self.store.source_turn_embedding_rows(
            missing_only=True, target_generation=generation,
        )
        self.store.replace_source_turn_embeddings(
            [(int(row["id"]), [0.1] * 4) for row in rows], runtime_gen=generation,
        )
        chunk_rows = self.store.source_turn_chunk_rows(
            missing_only=True, target_generation=generation,
        )
        self.store.replace_source_chunk_embeddings(
            [(int(row["id"]), [0.1] * 4) for row in chunk_rows], runtime_gen=generation,
        )
        card_rows = self.store.episode_card_embedding_rows(target_generation=generation)
        self.store.replace_episode_card_embeddings(
            [(int(row["id"]), [0.1] * 4) for row in card_rows], runtime_gen=generation,
        )

        async def preview(_episode_id):
            return {"preview_id": "p-web", "status": "pending"}
        async def confirm(_preview_id):
            return {"status": "confirmed", "ok": True}
        async def rollback(**_kwargs):
            return {"deleted": 0, "rows": 0, "errors": []}
        async def discard(preview_id):
            return {"discarded": True, "preview_id": preview_id}

        self.plugin = SimpleNamespace(
            webui_enable=True,
            webui_host="127.0.0.1",
            webui_port=0,
            _episodes=self.store,
            _emb_dim=4,
            _emb_model_id="model-b",
            _PLUGIN_VERSION="4.6.2",
            _log_events=[],
            _last_injection_stats=[],
            _long_diaries_list=lambda: [{"episode_id": "ep-web-one", "memo_name": "memos/web-one",
                                          "card_len": 4000, "source_batch_id": batch,
                                          "source_turns": 1, "must_coverage": 1.0,
                                          "transcript_risk": 0.1}],
            _create_diary_rewrite_preview=preview,
            _confirm_diary_rewrite=confirm,
            _rollback_diary_rewrite=rollback,
            _discard_diary_preview=discard,
        )
        self.server = WebUIServer(self.plugin)
        self.assertTrue(await self.server.start())
        self.port = int(self.server._server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        await self.server.stop()
        self.store.close()
        self.temp.cleanup()

    async def _get_json(self, path: str):
        return await asyncio.to_thread(
            lambda: json.loads(urllib.request.urlopen(self.base + path, timeout=5).read().decode("utf-8"))
        )

    async def _post_json(self, path: str, body: dict, origin: str | None = None):
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + path, data=payload, headers=headers, method="POST")
        return await asyncio.to_thread(
            lambda: json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
        )

    async def test_standalone_production_page_and_all_get_routes(self):
        html = await asyncio.to_thread(
            lambda: urllib.request.urlopen(self.base + "/", timeout=5).read().decode("utf-8")
        )
        self.assertIn('href="/production"', html)
        self.assertNotIn('data-tab="production"', html)
        production_html = await asyncio.to_thread(
            lambda: urllib.request.urlopen(self.base + "/production", timeout=5).read().decode("utf-8")
        )
        self.assertIn("记忆生产工作台", production_html)
        self.assertIn('id="episode-rows"', production_html)
        for path, key in (
            ("/api/production/overview", "ready"),
            ("/api/production/episodes", "items"),
            ("/api/production/snapshots", "snapshots"),
            ("/api/production/long_diaries", "long_diaries"),
            ("/api/production/rollbacks", "rollbacks"),
            ("/api/production/previews", "previews"),
        ):
            response = await self._get_json(path)
            self.assertTrue(response["ok"], (path, response))
            self.assertIn(key, response["data"])
        overview = (await self._get_json("/api/production/overview"))["data"]
        self.assertEqual(overview["source_status"]["level"], "yellow")
        self.assertEqual(overview["tier_dist"]["must_write"], 1)
        self.assertEqual(overview["trace"]["turn_linked_episodes"], 1)
        self.assertEqual(overview["traceable"]["recoverable_episodes"], 1)
        self.assertEqual(overview["traceable"]["episodes_source_grounded"], 0)
        self.assertEqual(overview["long_diaries_count"], 1)
        self.assertTrue(overview["generation_actions"]["switch_available"])
        episodes = (await self._get_json("/api/production/episodes"))["data"]
        self.assertEqual(episodes["total"], 1)
        row = episodes["items"][0]
        self.assertTrue(row["recoverable"])
        self.assertEqual(row["source_turns"], 1)
        self.assertAlmostEqual(row["compression_ratio"], 0.48)

        status = (await self._get_json("/api/episodic/status"))["data"]
        self.assertEqual(status["recoverable_episodes"], 1)

    async def test_generation_switch_and_rollback_posts_are_live(self):
        response = await self._post_json("/api/production/generation_switch", {})
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["data"]["switched"])
        self.assertEqual(self.store.stats()["active_generation"],
                         self.store._gen.runtime_generation("model-b", 4))
        rolled = await self._post_json("/api/production/generation_rollback", {})
        self.assertTrue(rolled["ok"], rolled)
        self.assertTrue(rolled["data"]["rolled_back"])
        self.assertEqual(self.store.stats()["active_generation"],
                         self.store._gen.runtime_generation("model-a", 3))

    async def test_preview_confirm_discard_and_rollback_posts(self):
        preview = await self._post_json(
            "/api/production/long_diary_preview", {"episode_id": "ep-web-one"}
        )
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["data"]["preview_id"], "p-web")
        confirmed = await self._post_json(
            "/api/production/long_diary_confirm", {"preview_id": "p-web"}
        )
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["data"]["status"], "confirmed")
        discarded = await self._post_json(
            "/api/production/long_diary_discard", {"preview_id": "p-web"}
        )
        self.assertTrue(discarded["ok"], discarded)
        self.assertTrue(discarded["data"]["discarded"])
        rolled = await self._post_json(
            "/api/production/long_diary_rollback", {"preview_id": "p-web"}
        )
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["data"]["deleted"], 0)

    async def test_snapshot_restore_post_is_live(self):
        snapshot = self.store.snapshot_db("manual")
        response = await self._post_json(
            "/api/production/restore_snapshot", {"file_name": snapshot["file_name"]}
        )
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["data"]["restored"])
        self.assertEqual(response["data"]["file_name"], snapshot["file_name"])

    async def test_cross_origin_post_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/production/generation_switch",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            await asyncio.to_thread(urllib.request.urlopen, request, None, 5)
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
