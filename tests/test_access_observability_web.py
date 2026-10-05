# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import tempfile
import urllib.request
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.access_export import AccessAnalysisExporter
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.webui import WebUIServer


class AccessObservabilityWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.temp.name) / "episodic.db"), 3, "unit")
        await self.store.init()
        episode = {
            "memo_name": "memos/web-access", "occurred_at": "2026-07-01 晚上",
            "event_ts": 1782835200.0, "memory_type": "plot_fact", "importance": 3,
            "scene_anchor": "雨夜天台", "retrieval_key": "银戒指 不离开",
            "entities": ["爱莉"], "card_text": "雨夜天台 银戒指 不离开",
        }
        self.store.upsert_episode(
            memo_name=episode["memo_name"], episode=episode,
            card_text=episode["card_text"], embedding=[1.0, 0.0, 0.0],
            evidence_quality="diary_derived",
        )
        self.store.rebuild_memory_access([episode])
        self.store.evaluate_memory_access(
            query="银戒指", candidates=[{"memo_name": "memos/web-access", "score": 0.7}],
            selected_names=["memos/web-access"], request_id="web-request",
            config={"shadow_mode": True}, record=True,
        )
        self.plugin = SimpleNamespace(
            webui_enable=True, webui_host="127.0.0.1", webui_port=0,
            _episodes=self.store, _PLUGIN_VERSION="6.1.0-rc5",
            _memory_access_state={"status": "ready"}, _memory_access_fail_open={"count": 0},
            memory_forgetting_enable=True, memory_forgetting_shadow_mode=True,
            memory_access_takeover_enable=False, memory_access_decay_days=45.0,
            memory_access_vivid_threshold=0.68, memory_access_deep_threshold=0.32,
            memory_access_exact_cue_relief=0.85, memory_access_observation_keep=5000,
            _access_export=AccessAnalysisExporter(str(Path(self.temp.name) / "exports")),
            _log_events=[], _last_injection_stats=[],
        )
        self.server = WebUIServer(self.plugin)
        self.assertTrue(await self.server.start())
        self.base = f"http://127.0.0.1:{int(self.server._server.server_address[1])}"

    async def asyncTearDown(self):
        await self.server.stop()
        self.store.close()
        self.temp.cleanup()

    async def get_json(self, path: str):
        return await asyncio.to_thread(
            lambda: json.loads(urllib.request.urlopen(self.base + path, timeout=5).read().decode("utf-8"))
        )

    async def post_json(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return await asyncio.to_thread(
            lambda: json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
        )

    async def test_page_and_observation_routes_are_live(self):
        html = await asyncio.to_thread(
            lambda: urllib.request.urlopen(self.base + "/access", timeout=5).read().decode("utf-8")
        )
        self.assertIn("真实 Shadow 评测流水", html)
        self.assertIn("6.1.0", html)
        listing = await self.get_json("/api/access/observations?limit=10")
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["data"]["items"][0]["request_id"], "web-request")
        detail = await self.get_json("/api/access/observation/web-request")
        self.assertEqual(detail["data"]["query_text"], "银戒指")
        feedback = await self.post_json("/api/access/observation_feedback", {
            "request_id": "web-request", "verdict": "shadow_better", "note": "测试反馈",
        })
        self.assertEqual(feedback["data"]["verdict"], "shadow_better")

    async def test_export_route_creates_and_downloads_zip(self):
        created = await self.post_json("/api/access/export", {})
        self.assertTrue(created["ok"])
        file_name = created["data"]["file"]
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(
                self.base + "/api/access/export/download?file=" + file_name, timeout=5,
            ).read()
        )
        zip_path = Path(self.temp.name) / "download.zip"
        zip_path.write_bytes(raw)
        with zipfile.ZipFile(zip_path) as archive:
            self.assertIn("manifest.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["plugin_version"], "6.1.0-rc5")
        self.assertEqual(manifest["counts"]["observations"], 1)


if __name__ == "__main__":
    unittest.main()
