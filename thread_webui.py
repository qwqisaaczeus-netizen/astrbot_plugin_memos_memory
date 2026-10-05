"""HTTP handlers for the 6.x Shadow context workbench."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .calibration import BASELINE, POLICY_ID, PRESETS
from .thread_policy import PRESETS as RUNTIME_PRESETS
from .thread_store import ThreadVersionConflict


class ThreadWebUIHandlers:
    def _serve_threads_page(self, handler: Any) -> None:
        path = Path(__file__).parent / "threads.html"
        content = path.read_bytes() if path.exists() else b"<h1>threads.html missing</h1>"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(content)))
        handler.end_headers()
        handler.wfile.write(content)

    @staticmethod
    def _episodes(plugin: Any) -> Any:
        episodes = getattr(plugin, "_episodes", None)
        if episodes is None or getattr(episodes, "_threads", None) is None:
            raise RuntimeError("thread layer not available")
        return episodes

    def _api_threads_status(self, handler: Any, plugin: Any) -> None:
        try:
            episodes = self._episodes(plugin)
            status = episodes.thread_status()
            runtime = getattr(plugin, "_llm_runtime", None)
            consistency_service = getattr(episodes, "consistency_service", None)
            consistency_stats = (
                consistency_service.stats() if consistency_service is not None else {
                    "mode": "unavailable", "closed": True,
                }
            )
            scope_id = str(getattr(plugin, "character_name", None) or "default")
            observations = episodes.thread_query_observations(scope_id=scope_id, limit=200)
            canary_items = [
                item for item in observations
                if isinstance(item.get("metrics"), dict) and item["metrics"].get("mode") in {"shadow", "canary"}
            ]
            selected = [item for item in canary_items if bool(item["metrics"].get("selected"))]
            injected = [item for item in canary_items if bool(item["metrics"].get("injected"))]
            fail_open = [item for item in canary_items if str(item["metrics"].get("outcome") or "") == "fail_open"]
            growth_values = [
                float(item["metrics"].get("growth_ratio") or 0) for item in canary_items
                if item["metrics"].get("growth_ratio") is not None
            ]
            status.update({
                "calibration": {
                    "policy_id": POLICY_ID,
                    "active_policy": BASELINE,
                    "offline_threshold_presets": PRESETS,
                    "runtime_presets": RUNTIME_PRESETS,
                    "auto_activate": False,
                },
                "derived_health": episodes._threads.health(),
                "background_work": (
                    plugin._background_work.status()
                    if getattr(plugin, "_background_work", None) is not None
                    else {"active": 0, "capacity": 0, "closed": False, "rejected": 0}
                ),
                "source_grades": episodes.thread_source_grade_counts(),
                "thread_memory_enable": bool(getattr(plugin, "thread_memory_enable", False)),
                "thread_mode": str(getattr(plugin, "thread_mode", "shadow")),
                "scope_id": scope_id,
                "plugin_version": str(getattr(plugin, "_PLUGIN_VERSION", "6.1.0")),
                "llm_arbitration_enable": bool(getattr(plugin, "thread_llm_arbitration_enable", False)),
                "daily_budget": int(getattr(plugin, "thread_llm_daily_budget", 0)),
                "upstream": {
                    "recall": "5.1_frozen_recall",
                    "baseline_stage": "5.1_intermediate",
                    "access_route_mode": str(getattr(plugin, "memory_access_route_mode", "shadow")),
                    "access_supplement_max": int(getattr(plugin, "memory_access_supplement_max", 0)),
                    "shared_llm_runtime": runtime is not None,
                },
                "canary": {
                    "mode": str(getattr(plugin, "thread_mode", "shadow")),
                    "percent": int(getattr(plugin, "thread_canary_percent", 10)),
                    "max_chars": int(getattr(plugin, "thread_canary_max_chars", 1200)),
                    "growth_percent": float(getattr(plugin, "thread_canary_growth_percent", 10.0)),
                    "timeout_ms": int(getattr(plugin, "thread_canary_timeout_ms", 300)),
                    "observed": len(canary_items),
                    "selected": len(selected),
                    "injected": len(injected),
                    "fail_open": len(fail_open),
                    "would_inject": sum(bool(item["metrics"].get("would_inject")) for item in canary_items),
                    "avg_growth_percent": round(sum(growth_values) * 100 / max(1, len(growth_values)), 2),
                    "avg_latency_ms": round(
                        sum(float(item.get("latency_ms") or 0) for item in canary_items) / max(1, len(canary_items)), 2
                    ),
                    "recent": [
                        {
                            "request_id": item.get("request_id"), "created_ts": item.get("created_ts"),
                            "query_text": item.get("query_text"), "latency_ms": item.get("latency_ms"),
                            "metrics": item.get("metrics"),
                        }
                        for item in canary_items[:30]
                    ],
                },
                "collection": {
                    "enabled": bool(getattr(plugin, "thread_memory_enable", True)),
                    "mode": "local_shadow",
                    "changes_requests": False,
                    "uploads_automatically": False,
                    "retention_days": int(getattr(plugin, "thread_observation_retention_days", 180)),
                    "worker_enabled": bool(getattr(plugin, "thread_worker_enable", True)),
                    "worker_running": bool(
                        getattr(plugin, "_thread_build_task", None) is not None
                        and not plugin._thread_build_task.done()
                    ),
                },
                "llm_runtime": runtime.snapshot() if runtime is not None else {},
                "consistency": consistency_stats,
            })
            _json_ok(handler, status)
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_pause(self, handler: Any, plugin: Any) -> None:
        try:
            self._episodes(plugin).thread_set_paused(True)
            _json_ok(handler, {"paused": True})
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_resume(self, handler: Any, plugin: Any) -> None:
        try:
            self._episodes(plugin).thread_set_paused(False)
            _json_ok(handler, {"paused": False})
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_edges(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            episode_id = _query(qs, "episode_id")
            scope_id = _query(qs, "scope_id")
            status = _query(qs, "status")
            limit = _query_int(qs, "limit", 100)
            episodes = self._episodes(plugin)
            _json_ok(handler, {
                "items": episodes.thread_list_edges(
                    episode_id=episode_id, scope_id=scope_id, status=status, limit=limit,
                ),
                "edge_counts": episodes.thread_edge_counts(),
            })
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_ambiguities(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            items = self._episodes(plugin).thread_list_ambiguity_queue(
                limit=_query_int(qs, "limit", 100)
            )
            _json_ok(handler, {"items": items})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_list(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            items = self._episodes(plugin).thread_list_threads(
                scope_id=_query(qs, "scope_id"), status=_query(qs, "status"),
                thread_type=_query(qs, "type"), limit=_query_int(qs, "limit", 200),
            )
            _json_ok(handler, {"items": items})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_detail(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            value = self._episodes(plugin).thread_detail(_query(qs, "id"))
            if value is None:
                return _json_err(handler, "thread not found", 404)
            value["current_view"] = self._episodes(plugin).thread_view(_query(qs, "id"))
            _json_ok(handler, value)
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_operations(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            items = self._episodes(plugin).thread_list_operations(
                scope_id=_query(qs, "scope_id"), limit=_query_int(qs, "limit", 50)
            )
            _json_ok(handler, {"items": items})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_claims(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            episodes = self._episodes(plugin)
            _json_ok(handler, {
                "items": episodes.thread_list_claims(
                    scope_id=_query(qs, "scope_id"), status=_query(qs, "status"),
                    claim_type=_query(qs, "type"), query=_query(qs, "query"),
                    limit=_query_int(qs, "limit", 500),
                ),
                "slots": episodes.thread_list_claim_slots(
                    scope_id=_query(qs, "scope_id"), limit=_query_int(qs, "limit", 500),
                ),
            })
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_claim_transitions(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_list_claim_transitions(
                thread_id=_query(qs, "thread_id"), claim_id=_query(qs, "claim_id"),
                limit=_query_int(qs, "limit", 200),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_view_history(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_view_history(
                _query(qs, "thread_id"), limit=_query_int(qs, "limit", 50),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_prospective(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_list_prospective(
                scope_id=_query(qs, "scope_id"), status=_query(qs, "status"),
                limit=_query_int(qs, "limit", 500),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_observations(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_query_observations(
                scope_id=_query(qs, "scope_id"), limit=_query_int(qs, "limit", 50),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_eval_cases(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_eval_cases(
                scope_id=_query(qs, "scope_id"), enabled_only=False,
                limit=_query_int(qs, "limit", 500),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_feedback(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            _json_ok(handler, {"items": self._episodes(plugin).thread_list_manual_feedback(
                scope_id=_query(qs, "scope_id"), target_id=_query(qs, "target_id"),
                limit=_query_int(qs, "limit", 200),
            )})
        except Exception as exc:
            _json_err(handler, str(exc))

    def _api_threads_consistency(self, handler: Any, plugin: Any, qs: dict) -> None:
        try:
            episodes = self._episodes(plugin)
            overview = episodes.thread_consistency_request_overview(
                request_id=_query(qs, "request_id"),
                scope_id=_query(qs, "scope_id"),
                observation_status=_enum_query(qs, "observation_status", OBSERVATION_STATUSES),
                consistency_status=_enum_query(qs, "consistency_status", CONSISTENCY_STATUSES),
                skip_reason=_enum_query(qs, "skip_reason", SKIP_REASONS),
                severity=_enum_query(qs, "severity", SEVERITIES),
                error_type=_query(qs, "error_type")[:120],
                limit=_strict_query_int(qs, "limit", 100),
            )
            overview.setdefault("summary", {})["service"] = (
                episodes.consistency_service.stats()
                if episodes.consistency_service is not None
                else {"mode": "unavailable", "closed": True}
            )
            _json_ok(handler, overview)
        except ValueError as exc:
            _json_err(handler, str(exc), 400)
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_consistency_detail(self, handler: Any, plugin: Any, request_id: str) -> None:
        try:
            detail = self._episodes(plugin).thread_consistency_detail(request_id)
            if detail.get("observation") is None and not detail.get("findings") and not detail.get("feedback"):
                return _json_err(handler, "request not found", 404)
            _json_ok(handler, detail)
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_consistency_export(self, handler: Any, plugin: Any, body: dict | None = None) -> None:
        try:
            service = getattr(self._episodes(plugin), "consistency_service", None)
            if service is None:
                raise RuntimeError("consistency service not available")
            body = body or {}
            try:
                raw_limit = body.get("limit", 500)
                if raw_limit in (None, ""):
                    raw_limit = 500
                limit = max(1, min(500, int(raw_limit)))
            except (TypeError, ValueError):
                return _json_err(handler, "invalid limit", 400)
            filters = {
                key: str(body.get(key) or "").strip()[:120]
                for key in ("request_id", "scope_id", "error_type")
                if str(body.get(key) or "").strip()
            }
            for key, allowed in (
                ("observation_status", OBSERVATION_STATUSES),
                ("consistency_status", CONSISTENCY_STATUSES),
                ("skip_reason", SKIP_REASONS),
                ("severity", SEVERITIES),
            ):
                value = str(body.get(key) or "").strip()
                if value:
                    if value not in allowed:
                        return _json_err(handler, f"invalid {key}", 400)
                    filters[key] = value
            _json_ok(handler, {"items": service.export(limit=limit, **filters)})
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_consistency_feedback(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            service = getattr(self._episodes(plugin), "consistency_service", None)
            if service is None:
                raise RuntimeError("consistency service not available")
            label = str(body.get("label") or "")
            result = service.feedback(str(body.get("request_id") or ""), label, note=str(body.get("note") or ""))
            _json_ok(handler, {"feedback_id": result})
        except ValueError as exc:
            _json_err(handler, str(exc), 400)
        except KeyError as exc:
            _json_err(handler, str(exc).strip("'"), 404)
        except Exception as exc:
            _json_err(handler, str(exc), 503)

    def _api_threads_decide(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            edge_id = int(body.get("edge_id") or 0)
            if edge_id <= 0:
                raise ValueError("edge_id is required")
            decision = {
                "decision": str(body.get("decision") or "uncertain"),
                "relation": str(body.get("relation") or "parallel"),
                "direction": str(body.get("direction") or "none"),
                "confidence": float(body.get("confidence", 1.0)),
                "reason_code": "manual_webui",
            }
            if decision["decision"] not in {"accepted", "rejected", "uncertain"}:
                raise ValueError("invalid decision")
            _json_ok(handler, self._episodes(plugin).thread_manual_edge_decision(edge_id, decision))
        except Exception as exc:
            _json_err(handler, str(exc), 400)

    def _api_threads_merge_preview(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_merge_preview(body.get("thread_ids") or []))

    def _api_threads_merge_apply(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_apply_merge(
            body.get("thread_ids") or [], expected_versions=body.get("expected_versions") or {},
        ))

    def _api_threads_split_preview(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_split_preview(
            str(body.get("thread_id") or ""), body.get("episode_ids") or [],
        ))

    def _api_threads_split_apply(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_apply_split(
            str(body.get("thread_id") or ""), body.get("episode_ids") or [],
            expected_version=body.get("expected_version"),
        ))

    def _api_threads_revert(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_revert_operation(
            str(body.get("operation_id") or ""),
        ))

    def _api_threads_claim_status(self, handler: Any, plugin: Any, body: dict) -> None:
        self._mutation(handler, lambda: self._episodes(plugin).thread_claim_manual_status(
            str(body.get("claim_id") or ""), str(body.get("status") or "uncertain"),
        ))

    def _api_threads_prospective_status(self, handler: Any, plugin: Any, body: dict) -> None:
        status = str(body.get("status") or "pending")
        cooldown_until = float(body.get("cooldown_until") or 0)
        if status == "snoozed" and cooldown_until <= time.time():
            cooldown_until = time.time() + 86400
        self._mutation(handler, lambda: self._episodes(plugin).thread_prospective_manual_status(
            str(body.get("item_id") or ""), status, cooldown_until=cooldown_until,
            reason=str(body.get("reason") or "webui"),
        ))

    def _api_threads_rebuild_derived(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            from .claim_ledger import ClaimLedger
            from .prospective_memory import ProspectiveMemory
            episodes = self._episodes(plugin)
            scope_id = str(body.get("scope_id") or getattr(plugin, "character_name", "") or "default")
            claims = ClaimLedger(episodes._threads).rebuild_scope(scope_id)
            prospective = ProspectiveMemory(
                episodes._threads,
                str(getattr(plugin, "rp_time_timezone", "Asia/Shanghai")),
            ).rebuild_scope(scope_id)
            _json_ok(handler, {"claims": claims, "prospective": prospective})
        except Exception as exc:
            _json_err(handler, str(exc), 500)

    def _api_threads_prospective_simulate(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            from .prospective_memory import ProspectiveMemory
            episodes = self._episodes(plugin)
            result = ProspectiveMemory(
                episodes._threads,
                str(getattr(plugin, "rp_time_timezone", "Asia/Shanghai")),
            ).trigger(
                str(body.get("scope_id") or getattr(plugin, "character_name", "") or "default"),
                str(body.get("query") or ""), context_text=str(body.get("context_text") or ""),
                emotion_signal=float(body.get("emotion_signal") or 0),
                now_ts=float(body.get("now_ts") or time.time()), record=True,
            )
            _json_ok(handler, result)
        except Exception as exc:
            _json_err(handler, str(exc), 400)

    def _api_threads_lab(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            query = str(body.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            lab_kwargs: dict[str, Any] = {
                "context_text": str(body.get("context_text") or ""),
                "mode": str(body.get("mode") or "full"),
            }
            if isinstance(body.get("query_plan"), dict):
                lab_kwargs["query_plan"] = body["query_plan"]
            if "emotion_signal" in body:
                lab_kwargs["emotion_signal"] = float(body.get("emotion_signal") or 0.0)
            result = self._await_plugin(
                plugin,
                plugin._run_thread_retrieval_lab(query, **lab_kwargs),
                timeout=180,
            )
            _json_ok(handler, result)
        except Exception as exc:
            _json_err(handler, str(exc), 400)

    def _api_threads_eval_run(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            result = self._await_plugin(
                plugin, plugin._run_thread_eval(case_limit=int(body.get("case_limit") or 120)),
                timeout=900,
            )
            _json_ok(handler, result)
        except Exception as exc:
            _json_err(handler, str(exc), 400)

    def _api_threads_lab_feedback(self, handler: Any, plugin: Any, body: dict) -> None:
        try:
            scope_id = str(body.get("scope_id") or getattr(plugin, "character_name", "") or "default")
            feedback_id = self._episodes(plugin).thread_record_manual_feedback(
                scope_id=scope_id,
                target_type="query_observation",
                target_id=str(body.get("observation_id") or ""),
                action=str(body.get("action") or "uncertain"),
                note=str(body.get("note") or ""),
            )
            _json_ok(handler, {"feedback_id": feedback_id})
        except (KeyError, ValueError) as exc:
            _json_err(handler, str(exc), 400)
        except Exception as exc:
            _json_err(handler, str(exc), 500)

    @staticmethod
    def _await_plugin(plugin: Any, coro: Any, *, timeout: float) -> Any:
        loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
        if not loop or not loop.is_running():
            coro.close()
            raise RuntimeError("AstrBot event loop not available")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    @staticmethod
    def _mutation(handler: Any, fn: Any) -> None:
        try:
            _json_ok(handler, fn())
        except ThreadVersionConflict as exc:
            _json_err(handler, str(exc), 409, code_name="version_conflict", extra={
                "thread_id": exc.thread_id, "current_version": exc.current_version,
            })
        except (KeyError, ValueError, RuntimeError) as exc:
            _json_err(handler, str(exc), 400)
        except Exception as exc:
            _json_err(handler, str(exc))


OBSERVATION_STATUSES = {"", "pending", "checked", "skipped"}
CONSISTENCY_STATUSES = {"", "pending", "clean", "flagged", "review", "skipped"}
SKIP_REASONS = {"", "missing_snapshot", "missing_response", "incomplete_snapshot", "queue_full", "service_closed", "local_timeout"}
SEVERITIES = {"", "critical", "high", "medium", "low"}


def _query(qs: dict, key: str) -> str:
    value = qs.get(key) or [""]
    return str(value[0] or "")


def _strict_query_int(qs: dict, key: str, default: int) -> int:
    raw = _query(qs, key)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {key}")
    if value < 1 or value > 1000:
        raise ValueError(f"invalid {key}")
    return value


def _enum_query(qs: dict, key: str, allowed: set[str]) -> str:
    value = _query(qs, key).strip()
    if value and value not in allowed:
        raise ValueError(f"invalid {key}")
    return value


def _query_int(qs: dict, key: str, default: int) -> int:
    try:
        return int(_query(qs, key) or default)
    except ValueError:
        return default


def _json_ok(handler: Any, data: Any) -> None:
    body = json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _json_err(handler: Any, message: str, code: int = 500, *, code_name: str = "", extra: dict[str, Any] | None = None) -> None:
    payload = {"ok": False, "error": str(message)}
    if code_name:
        payload["code"] = code_name
    if extra:
        payload.update(extra)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
