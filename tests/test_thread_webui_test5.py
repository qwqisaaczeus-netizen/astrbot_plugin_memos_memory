# -*- coding: utf-8 -*-
"""Live HTTP route coverage for the 6.0-test5 context workbench."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.webui import WebUIServer


class ThreadWebUITest5(unittest.IsolatedAsyncioTestCase):
    async def test_page_get_routes_and_mutation_routes_are_live(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodic.db"), 3, "unit")
            await store.init()
            now = time.time()
            store.upsert_episode(
                memo_name="memos/webui",
                episode={
                    "occurred_at": "2026-08-23 19:00", "event_ts": now,
                    "time_basis": "source_turn", "memory_type": "promise_or_rule", "importance": 4,
                    "scene_anchor": "天台", "retrieval_key": "我答应爱莉明天去天台看星星",
                    "state_change": "我答应爱莉明天去天台看星星", "long_effect": "",
                    "trigger_hint": "天台 星星", "entities": ["爱莉"], "unresolved": [],
                },
                card_text="我答应明天去天台看星星。", embedding=[1.0, 0.0, 0.0],
                evidence_quality="source_grounded", source_batch_id="",
            )
            episode_id = str(store.get_episode("memos/webui")["episode_id"])
            store.thread_enqueue("role-web", episode_id)
            for request_id, scope_id in (("visible-observation", "role-web"), ("hidden-observation", "role-other")):
                store._threads.record_thread_query_observation({
                    "request_id": request_id,
                    "scope_id": scope_id,
                    "query_text": request_id,
                    "metrics": {"mode": "shadow", "selected": False, "injected": False},
                })

            async def fake_lab(query, context_text="", mode="full"):
                return {
                    "shadow": True, "query": query, "mode": mode, "context": context_text,
                    "observation_id": "thread_lab_webui",
                }

            async def fake_eval(case_limit=120):
                return {"shadow": True, "cases": min(2, case_limit), "human_labels": 0}

            plugin = SimpleNamespace(
                _episodes=store, _PLUGIN_VERSION="6.1.0-rc5", character_name="role-web",
                thread_memory_enable=True, thread_mode="shadow",
                thread_llm_arbitration_enable=True, thread_llm_daily_budget=40,
                webui_enable=True, webui_host="127.0.0.1", webui_port=0,
                _run_thread_retrieval_lab=fake_lab, _run_thread_eval=fake_eval,
            )
            server = WebUIServer(plugin)
            plugin._webui = server
            self.assertTrue(await server.start())
            base = f"http://127.0.0.1:{server._server.server_address[1]}"

            def get(path):
                with urllib.request.urlopen(base + path, timeout=4) as response:
                    payload = response.read().decode("utf-8")
                    return payload if not path.startswith("/api/") else json.loads(payload)

            def post(path, body):
                request = urllib.request.Request(
                    base + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                html = await asyncio.to_thread(get, "/threads")
                for marker in ("当前事实", "前瞻事项", "检索实验室", "本次结果反馈", "仅5.1中间主路", "Canary 观测"):
                    self.assertIn(marker, html)
                status = await asyncio.to_thread(get, "/api/threads/status")
                self.assertTrue(status["ok"])
                self.assertEqual(status["data"]["plugin_version"], "6.1.0-rc5")
                self.assertEqual(status["data"]["upstream"]["recall"], "5.1_frozen_recall")
                self.assertEqual(status["data"]["upstream"]["baseline_stage"], "5.1_intermediate")
                self.assertIn("canary", status["data"])
                self.assertEqual(status["data"]["scope_id"], "role-web")
                self.assertEqual(status["data"]["canary"]["observed"], 1)
                self.assertEqual(status["data"]["canary"]["recent"][0]["request_id"], "visible-observation")
                self.assertEqual(status["data"]["calibration"]["policy_id"], "test9-frozen-test8-v2")
                self.assertFalse(status["data"]["calibration"]["auto_activate"])

                rebuilt = await asyncio.to_thread(
                    post, "/api/threads/rebuild-derived", {"scope_id": "role-web"}
                )
                self.assertTrue(rebuilt["ok"])
                self.assertGreaterEqual(rebuilt["data"]["claims"]["extracted"], 1)
                claims = await asyncio.to_thread(get, "/api/threads/claims?scope_id=role-web")
                prospective = await asyncio.to_thread(get, "/api/threads/prospective?scope_id=role-web")
                self.assertTrue(claims["data"]["items"])
                self.assertTrue(prospective["data"]["items"])

                claim_id = claims["data"]["items"][0]["claim_id"]
                changed = await asyncio.to_thread(
                    post, "/api/threads/claim/status", {"claim_id": claim_id, "status": "active"}
                )
                self.assertTrue(changed["ok"])
                item_id = prospective["data"]["items"][0]["item_id"]
                snoozed = await asyncio.to_thread(
                    post, "/api/threads/prospective/status", {"item_id": item_id, "status": "snoozed"}
                )
                self.assertEqual(snoozed["data"]["item"]["status"], "snoozed")

                simulated = await asyncio.to_thread(
                    post, "/api/threads/prospective/simulate",
                    {"scope_id": "role-web", "query": "爱莉去天台看星星", "context_text": "明天约定"},
                )
                self.assertTrue(simulated["data"]["shadow"])
                lab = await asyncio.to_thread(
                    post, "/api/threads/lab", {"query": "关系以前怎么变的", "mode": "no_claims"}
                )
                self.assertEqual(lab["data"]["mode"], "no_claims")
                feedback = await asyncio.to_thread(
                    post, "/api/threads/lab/feedback",
                    {"observation_id": lab["data"]["observation_id"], "action": "missed"},
                )
                self.assertGreater(feedback["data"]["feedback_id"], 0)
                evaluated = await asyncio.to_thread(
                    post, "/api/threads/eval/run", {"case_limit": 10}
                )
                self.assertEqual(evaluated["data"]["human_labels"], 0)

                store.thread_record_consistency_result(
                    "web-clean", [], scope_id="role-web", query_text="今天状态如何",
                    observation_status="checked", consistency_status="clean",
                    snapshot_complete=1, thread_used=0, response_status="completed",
                )
                store.thread_record_consistency_result(
                    "web-skip", [], scope_id="role-web", query_text="缺少回答",
                    observation_status="skipped", consistency_status="skipped",
                    skip_reason="missing_response", snapshot_complete=1, thread_used=1,
                )
                store.thread_record_consistency_result(
                    "web-flag", [{
                        "error_type": "date_conflict", "description": "日期冲突",
                        "severity": "high", "confidence": .9,
                        "evidence": {"source_id": "private-source", "text": "私密证据"},
                    }], scope_id="role-web", query_text="私密查询",
                    answer_preview="私密回答", observation_status="checked",
                    consistency_status="flagged", snapshot_complete=1, thread_used=1,
                    response_status="completed",
                )
                consistency = await asyncio.to_thread(
                    get, "/api/threads/consistency?scope_id=role-web&limit=10"
                )
                self.assertEqual(consistency["data"]["summary"]["requests"], 3)
                self.assertEqual(consistency["data"]["summary"]["status"]["clean"], 1)
                self.assertEqual(consistency["data"]["summary"]["status"]["skipped"], 1)
                self.assertEqual(consistency["data"]["summary"]["status"]["flagged"], 1)
                self.assertIn("dropped", consistency["data"]["summary"]["service"])
                skipped = await asyncio.to_thread(
                    get, "/api/threads/consistency?scope_id=role-web&skip_reason=missing_response"
                )
                self.assertEqual([item["request_id"] for item in skipped["data"]["items"]], ["web-skip"])
                checked = await asyncio.to_thread(
                    get, "/api/threads/consistency?scope_id=role-web&observation_status=checked&consistency_status=flagged"
                )
                self.assertEqual([item["request_id"] for item in checked["data"]["items"]], ["web-flag"])
                with self.assertRaises(urllib.error.HTTPError) as invalid_filter:
                    await asyncio.to_thread(
                        get, "/api/threads/consistency?scope_id=role-web&severity=not-a-severity"
                    )
                self.assertEqual(invalid_filter.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as invalid_limit:
                    await asyncio.to_thread(
                        get, "/api/threads/consistency?scope_id=role-web&limit=abc"
                    )
                self.assertEqual(invalid_limit.exception.code, 400)
                detail = await asyncio.to_thread(
                    get, "/api/threads/consistency/detail/web-flag"
                )
                self.assertEqual(detail["data"]["observation"]["query_text"], "私密查询")
                self.assertEqual(detail["data"]["findings"][0]["error_type"], "date_conflict")
                marked = await asyncio.to_thread(
                    post, "/api/threads/consistency/feedback",
                    {"request_id": "web-flag", "label": "false_positive", "note": "已核对"},
                )
                self.assertGreater(marked["data"]["feedback_id"], 0)
                detail = await asyncio.to_thread(
                    get, "/api/threads/consistency/detail/web-flag"
                )
                self.assertEqual(detail["data"]["feedback"][0]["label"], "false_positive")
                exported = await asyncio.to_thread(
                    post, "/api/threads/consistency/export",
                    {"scope_id": "role-web", "observation_status": "checked",
                     "consistency_status": "flagged", "severity": "high", "limit": 10},
                )
                self.assertEqual(len(exported["data"]["items"]), 1)
                dumped = json.dumps(exported["data"], ensure_ascii=False)
                for secret in ("private-source", "私密证据", "私密查询", "私密回答", "web-flag"):
                    self.assertNotIn(secret, dumped)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    await asyncio.to_thread(
                        get, "/api/threads/consistency/detail/not-found"
                    )
                self.assertEqual(missing.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as invalid_feedback:
                    await asyncio.to_thread(
                        post, "/api/threads/consistency/feedback",
                        {"request_id": "web-flag", "label": "invalid"},
                    )
                self.assertEqual(invalid_feedback.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as missing_feedback:
                    await asyncio.to_thread(
                        post, "/api/threads/consistency/feedback",
                        {"request_id": "not-found", "label": "uncertain"},
                    )
                self.assertEqual(missing_feedback.exception.code, 404)

                for path in (
                    "/api/threads/edges", "/api/threads/ambiguities", "/api/threads/list",
                    "/api/threads/operations", "/api/threads/claim-transitions",
                    "/api/threads/observations", "/api/threads/eval-cases", "/api/threads/feedback",
                ):
                    payload = await asyncio.to_thread(get, path)
                    self.assertTrue(payload["ok"], path)
            finally:
                await server.stop()
                store.close()


if __name__ == "__main__":
    unittest.main()
