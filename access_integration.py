"""AstrBot lifecycle integration for the 5.1 ACCESS derivation and supplement route."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MemoryAccessIntegrationMixin:
    def _memory_access_config(self) -> dict[str, Any]:
        return {
            "enable": self.memory_forgetting_enable,
            "shadow_mode": self.memory_forgetting_shadow_mode,
            "decay_enable": self.memory_access_decay_enable,
            "deep_rescue_enable": self.memory_access_deep_rescue_enable,
            "independent_cue_rescue": self.memory_access_independent_cue_rescue,
            "rescue_candidate_limit": self.memory_access_rescue_candidate_limit,
            "interference_enable": self.memory_interference_enable,
            "reconsolidation_enable": self.memory_reconsolidation_enable,
            "psychological_bias_enable": self.memory_psychological_bias_enable,
            "psychological_bias_strength": self.memory_psychological_bias_strength,
            "vivid_threshold": self.memory_access_vivid_threshold,
            "deep_threshold": self.memory_access_deep_threshold,
            "decay_days": self.memory_access_decay_days,
            "exact_cue_relief": self.memory_access_exact_cue_relief,
            "max_neighbors": self.memory_access_max_neighbors,
            "event_keep": self.memory_access_event_keep,
            "observation_keep": self.memory_access_observation_keep,
            "observation_query_max_chars": self.memory_access_observation_query_max_chars,
            "cue_grade_b_rarity_min": self.memory_access_cue_grade_b_rarity_min,
            "same_day_gap_threshold": self.memory_access_same_day_gap_threshold,
            "state_confirmation_runs": self.memory_access_state_confirmation_runs,
            "source_proxy_weight": self.memory_access_source_proxy_weight,
            "route_mode": self.memory_access_route_mode,
            "supplement_max": self.memory_access_supplement_max,
            "supplement_temporal_max": self.memory_access_supplement_temporal_max,
            "supplement_rescue_max": self.memory_access_supplement_rescue_max,
            "supplement_temporal_threshold": self.memory_access_supplement_temporal_threshold,
            "supplement_rescue_threshold": self.memory_access_supplement_rescue_threshold,
            "supplement_source_bonus": self.memory_access_supplement_source_bonus,
            "supplement_allow_diary_derived": self.memory_access_supplement_allow_diary_derived,
            "supplement_holdout_percent": self.memory_access_supplement_holdout_percent,
            "takeover_enable": self.memory_access_takeover_enable,
            "takeover_max_appends": self.memory_access_takeover_max_appends,
            "takeover_min_grade": self.memory_access_takeover_min_grade,
            "takeover_source_lexical_min": self.memory_access_takeover_source_lexical_min,
            "takeover_breaker_threshold": self.memory_access_takeover_breaker_threshold,
            "takeover_min_eval_cases": self.memory_access_takeover_min_eval_cases,
            "takeover_source_min_eval_cases": self.memory_access_takeover_source_min_eval_cases,
            "eval_max_age_days": self.memory_access_eval_max_age_days,
            "auto_eval_case_limit": self.memory_access_auto_eval_case_limit,
        }

    def _all_episode_records(self) -> list[dict[str, Any]]:
        if self._episodes is None:
            return []
        output: list[dict[str, Any]] = []
        offset = 0
        while True:
            rows = self._episodes.list_episodes(limit=500, offset=offset)
            output.extend(rows)
            if len(rows) < 500:
                break
            offset += len(rows)
        return output

    async def _memory_access_rebuild(self, reason: str = "manual") -> dict[str, Any]:
        if self._episodes is None:
            return {"updated": 0, "reason": "episode_store_unavailable"}
        episodes = self._all_episode_records()
        result = await self._run_background_work(
            self._episodes.rebuild_memory_access,
            episodes,
            config=self._memory_access_config(),
            reason=reason,
            reuse_interference=(reason == "startup_backfill"),
        )
        self._memory_access_state = {
            "status": "ready", "updated_ts": time.time(), "reason": reason, **result,
        }
        self._log_event(
            "access",
            (
                f"可达性派生层已同步: {result.get('updated', 0)} 条 / "
                f"{result.get('edges', 0)} 条干扰边 / "
                f"图谱={'复用' if result.get('graph_reused') else '重建'}"
            ),
            self._memory_access_state,
        )
        return result

    async def _drain_memory_interference_dirty(self, *, max_items: int = 250) -> dict[str, int]:
        """Consume queued graph updates without blocking AstrBot's event loop."""
        totals = {"batch": 0, "retry": 0, "neighbours": 0, "edges": 0}
        if self._episodes is None:
            return totals
        remaining_budget = max(1, int(max_items))
        while remaining_budget > 0:
            batch = await self._run_background_work(
                self._episodes.pop_memory_interference_dirty, min(50, remaining_budget)
            )
            if not batch:
                break
            batch_result = await self._run_background_work(
                self._episodes.sync_memory_interference_batch,
                batch,
                max_neighbors=self.memory_access_max_neighbors,
            )
            retry = list(batch_result.get("failed") or [])
            if retry:
                await self._run_background_work(self._episodes.mark_memory_interference_dirty, retry)
            totals["batch"] += len(batch)
            totals["retry"] += len(retry)
            totals["neighbours"] += int(batch_result.get("neighbours") or 0)
            totals["edges"] += int(batch_result.get("edges") or 0)
            remaining_budget -= len(batch)
            await asyncio.sleep(0)
            if retry and len(retry) == len(batch):
                break
        if totals["batch"]:
            self._log_event(
                "access",
                (
                    f"增量干扰维护: batch={totals['batch']} retry={totals['retry']} "
                    f"neighbours={totals['neighbours']} edges={totals['edges']}"
                ),
                totals,
            )
        return totals

    async def _memory_access_maintenance_loop(self) -> None:
        try:
            migration = self._episode_migration_task
            if migration is not None:
                try:
                    await asyncio.shield(migration)
                except Exception as exc:
                    logger.warning("[memos-memory][access] episode bridge incomplete; rebuilding current rows: %s", exc)
            await self._memory_access_rebuild("startup_backfill")
            await self._drain_memory_interference_dirty()
            event = self._memory_access_dirty_event or asyncio.Event()
            self._memory_access_dirty_event = event
            interval = max(60.0, float(self.memory_access_maintenance_hours) * 3600.0)
            next_maintenance = time.monotonic() + interval
            while True:
                timeout = max(1.0, next_maintenance - time.monotonic())
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                    event.clear()
                    # Coalesce a sync burst without delaying normal chat requests.
                    await asyncio.sleep(2.0)
                    event.clear()
                    await self._drain_memory_interference_dirty()
                except asyncio.TimeoutError:
                    pass
                if self._episodes is None:
                    continue
                if time.monotonic() < next_maintenance:
                    continue
                try:
                    await self._drain_memory_interference_dirty()
                    result = await self._run_background_work(
                        self._episodes.maintain_memory_access,
                        config=self._memory_access_config(),
                        reason="scheduled",
                    )
                    self._memory_access_state = {
                        "status": "ready", "updated_ts": time.time(),
                        "reason": "scheduled", **result,
                    }
                except Exception as exc:
                    self._record_memory_access_fail_open("scheduled_maintenance", exc)
                    self._memory_access_state = {
                        "status": "failed_open", "updated_ts": time.time(), "error": str(exc)[:300],
                    }
                    logger.warning("[memos-memory][access] maintenance failed open: %s", exc)
                finally:
                    next_maintenance = time.monotonic() + interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_memory_access_fail_open("startup_backfill", exc)
            self._memory_access_state = {
                "status": "failed_open", "updated_ts": time.time(), "error": str(exc)[:300],
            }
            logger.warning("[memos-memory][access] bootstrap failed open; 4.6 recall remains active: %s", exc)


    @staticmethod
    def _memory_access_psychological_bias(event: AstrMessageEvent) -> float:
        try:
            appraisal = event.get_extra("xinchao_live_appraisal", {}) or {}
        except Exception:
            appraisal = {}
        if not isinstance(appraisal, dict):
            return 0.0
        levels = appraisal.get("activationLevels") or {}
        if not isinstance(levels, dict):
            return 0.0
        values = []
        for value in levels.values():
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        # This is only a bounded salience hint in test0. It cannot veto or
        # rescue a memory by itself and remains observational in Shadow mode.
        return max(0.0, min(1.0, max(values or [0.0]))) * 0.05

    def _record_memory_access_fail_open(self, stage: str, exc: Exception) -> None:
        state = dict(getattr(self, "_memory_access_fail_open", {}) or {})
        state["count"] = int(state.get("count") or 0) + 1
        state["last_stage"] = str(stage or "unknown")[:80]
        state["last_reason"] = str(exc)[:300]
        state["last_ts"] = time.time()
        self._memory_access_fail_open = state

    def _evaluate_memory_access_shadow(
        self,
        event: AstrMessageEvent,
        query: str,
        candidates: list[dict[str, Any]],
        selected_hits: list[dict[str, Any]],
        request_stat: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.memory_forgetting_enable or self._episodes is None:
            return {"enabled": False, "shadow": True, "reason": "disabled_or_unavailable"}
        try:
            selected_names = [
                str(item.get("memo_name") or "") for item in selected_hits
                if str(item.get("memo_name") or "")
            ]
            result = self._episodes.evaluate_memory_access(
                query=query,
                candidates=candidates,
                selected_names=selected_names,
                request_id=str(request_stat.get("request_id") or ""),
                config=self._memory_access_config(),
                psychological_bias=(
                    self._memory_access_psychological_bias(event)
                    if self.memory_psychological_bias_enable else 0.0
                ),
                record=True,
            )
            self._log_event(
                "access",
                (
                    f"Shadow 对照: candidates={result.get('candidate_count', 0)} "
                    f"selected={result.get('selected_count', 0)} deep_rescue={result.get('deep_rescue_count', 0)} "
                    f"would_change={bool(result.get('would_change'))}"
                ),
                {key: value for key, value in result.items() if key != "items"},
            )
            return result
        except Exception as exc:
            self._record_memory_access_fail_open("request_evaluation", exc)
            logger.warning("[memos-memory][access] request evaluation failed open: %s", exc)
            return {"enabled": True, "shadow": True, "failed_open": True, "error": str(exc)[:300]}

    def _record_memory_access_response(self, event: AstrMessageEvent, response_text: str) -> None:
        if not self.memory_forgetting_enable or self._episodes is None or not response_text:
            return
        try:
            request_id = str(event.get_extra("memos_memory_request_id", "") or "")
        except Exception:
            request_id = ""
        if not request_id:
            return
        stat = next((
            item for item in reversed(self._last_injection_stats)
            if str(item.get("request_id") or "") == request_id
        ), None)
        if not stat:
            return
        memo_names = [str(item) for item in stat.get("memos") or [] if str(item)]
        try:
            result = self._episodes.record_memory_response_use(
                request_id=request_id,
                response_text=response_text,
                memo_names=memo_names,
                shadow=self.memory_forgetting_shadow_mode,
                reconsolidate=self.memory_reconsolidation_enable,
                event_keep=self.memory_access_event_keep,
            )
            stat["memory_access_response"] = result
            self._remember_injection_stats(stat)
            # test3: evaluate takeover response use and trip breaker if needed
            if self.memory_access_takeover_enable:
                try:
                    takeover_result = self._episodes.evaluate_memory_takeover_response_use(
                        request_id=request_id, response_text=response_text,
                        reconsolidate=self.memory_reconsolidation_enable,
                    )
                    unused = int(takeover_result.get("unused") or 0)
                    if unused > 0:
                        breaker = self._episodes.memory_access_breaker_status(
                            config=self._memory_access_config(),
                        )
                        if breaker.get("tripped"):
                            self._episodes.memory_access_trip_breaker(
                                reason=f"consecutive_unused={breaker.get('consecutive_unused')}",
                                route_mode=self.memory_access_route_mode,
                            )
                            logger.warning("[memos-memory][access] takeover breaker tripped: %s",
                                           breaker.get("consecutive_unused"))
                except Exception as exc:
                    logger.debug("[memos-memory][access] takeover response check failed open: %s", exc)
        except Exception as exc:
            logger.debug("[memos-memory][access] response observation failed open: %s", exc)

    def _materialize_memory_access_hit(
        self,
        memo_name: str,
        candidates: list[dict[str, Any]],
        access_item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build an injectable hit for an access-layer append, including pool-external rescues."""
        memo_name = str(memo_name or "").strip()
        if not memo_name:
            return None
        existing = next(
            (item for item in candidates if str(item.get("memo_name") or "") == memo_name),
            None,
        )
        if existing is not None:
            hit = dict(existing)
        else:
            episode = self._episodes.get_episode(memo_name) if self._episodes is not None else None
            if not episode:
                return None
            hit = {
                "memo_name": memo_name,
                "chunk_text": str(episode.get("card_text") or episode.get("scene_anchor") or ""),
                "ts_text": str(episode.get("occurred_at") or ""),
                "occurred_at": str(episode.get("occurred_at") or ""),
                "event_ts": float(episode.get("event_ts") or 0),
                "time_basis": str(episode.get("time_basis") or "unknown"),
                "memory_type": str(episode.get("memory_type") or "plot_fact"),
                "importance": int(episode.get("importance") or 3),
                "scene_anchor": str(episode.get("scene_anchor") or ""),
                "retrieval_key": str(episode.get("retrieval_key") or ""),
                "state_change": str(episode.get("state_change") or ""),
                "long_effect": str(episode.get("long_effect") or ""),
                "trigger_hint": str(episode.get("trigger_hint") or ""),
                "entities": episode.get("entities") or [],
                "_episodic": True,
                "_episode_id": str(episode.get("episode_id") or ""),
                "_evidence_quality": str(episode.get("evidence_quality") or "diary_derived"),
                "_affect_before": str(episode.get("affect_before") or ""),
                "_affect_after": str(episode.get("affect_after") or ""),
                "_unresolved": episode.get("unresolved") or [],
            }
        access_item = dict(access_item or {})
        hit["score"] = max(
            float(hit.get("score") or 0.0), float(access_item.get("access_score") or 0.0)
        )
        hit["relevance"] = max(
            float(hit.get("relevance") or 0.0), float(access_item.get("access_score") or 0.0)
        )
        hit["_selected"] = True
        hit["_access_takeover"] = True
        hit["_access_takeover_reason"] = str(access_item.get("reason") or "")
        hit["_access_takeover_presentation"] = str(
            access_item.get("presentation") or "compact_source_evidence"
        )
        source_route = access_item.get("source_route") or {}
        source_hits = [
            dict(item) for item in (access_item.get("source_turn_hits") or [])
            if isinstance(item, dict)
        ]
        if source_hits:
            hit["_source_turn_hits"] = source_hits[:2]
            hit["_source_evidence_hit"] = True
        hit["_access_source_route"] = dict(source_route) if isinstance(source_route, dict) else {}
        return hit
