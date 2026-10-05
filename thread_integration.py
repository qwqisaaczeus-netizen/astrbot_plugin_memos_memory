"""AstrBot lifecycle integration for the 6.x thread-memory shadow layer.

The storage and retrieval algorithms live in their own focused modules.  This
mixin only coordinates them with the plugin lifecycle, the 5.1 ACCESS baseline,
and the shared LLM runtime.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from astrbot.api import logger


def _coerce_emotion_signal(value: Any, default: float = 0.0) -> float:
    """Normalize the optional, caller-supplied emotion signal without I/O."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, min(1.0, value)) if math.isfinite(value) else float(default)


def _resolve_emotion_signal(plan: Any, explicit: Any = None) -> float:
    """Use an explicit signal first, then one already carried by the plan."""
    if explicit is not None:
        return _coerce_emotion_signal(explicit)
    planned = plan.get("emotion_signal", 0.0) if isinstance(plan, dict) else getattr(plan, "emotion_signal", 0.0)
    return _coerce_emotion_signal(planned)


def _provider_display_name(provider: Any) -> str:
    if provider is None:
        return "unknown"
    if hasattr(provider, "get_model"):
        try:
            model = provider.get_model()
            if model and str(model).strip():
                return str(model).strip()
        except Exception:
            pass
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict) and str(config.get("type") or "").strip():
        return str(config["type"]).strip()
    return str(getattr(provider, "provider_id", None) or "unknown").strip()


class ThreadIntegrationMixin:
    """Coordinate thread derivation without changing the established recall path."""

    def _thread_effective_mode(self):
        from .thread_policy import gates
        mode = str(getattr(self, "thread_mode", "shadow"))
        if mode == "active" and not gates(self)["active_qualified"]:
            return "shadow"
        return mode if mode in {"shadow", "canary", "active"} else "shadow"

    def _thread_node_limits(self):
        config = getattr(self, "config", {})
        return {"max_nodes": int(getattr(self, "thread_subgraph_nodes", config.get("thread_subgraph_nodes", 5))),
                "evolution_nodes": int(getattr(self, "thread_evolution_nodes", config.get("thread_evolution_nodes", 7)))}

    def _apply_thread_runtime_policy(self) -> bool:
        """Start or stop the derived-data worker after a WebUI policy change."""
        loop = getattr(getattr(self, "_webui", None), "_loop", None)
        if loop is None or not loop.is_running():
            return False

        def apply() -> None:
            should_run = bool(
                getattr(self, "thread_memory_enable", False)
                and getattr(self, "thread_worker_enable", False)
                and getattr(self, "_episodes", None) is not None
                and not getattr(self, "_terminating", False)
            )
            current = getattr(self, "_thread_build_task", None)
            if should_run and (current is None or current.done()):
                self._thread_build_task = loop.create_task(self._thread_build_loop())
                logger.info("[memos-memory][thread] background worker started by policy")
                return
            if not should_run and current is not None and not current.done():
                current.cancel()

                def clear(done: asyncio.Task[Any]) -> None:
                    if getattr(self, "_thread_build_task", None) is done:
                        self._thread_build_task = None
                    if (
                        getattr(self, "thread_memory_enable", False)
                        and getattr(self, "thread_worker_enable", False)
                        and not getattr(self, "_terminating", False)
                    ):
                        self._apply_thread_runtime_policy()

                current.add_done_callback(clear)
                logger.info("[memos-memory][thread] background worker stopping by policy")

        loop.call_soon_threadsafe(apply)
        return True

    async def _run_background_work(self, fn, *args, **kwargs):
        from .background_work import BackgroundWork
        tracker = getattr(self, "_background_work", None)
        if tracker is None:
            tracker = self._background_work = BackgroundWork(capacity=4)
        return await tracker.run(fn, *args, **kwargs)

    def _thread_refresh_candidates(self):
        from .thread_candidates import ThreadCandidates
        with self._episodes._lock:
            return ThreadCandidates(self._episodes._connect).refresh_dirty_batch(64)

    @staticmethod
    def _thread_worker_next_delay(
        interval: float,
        *,
        processed: int,
        batch_size: int,
        errors: int,
        pending: int,
    ) -> float:
        """Drain a healthy backlog promptly, then return to the quiet cadence."""
        normal = max(60.0, float(interval))
        if (
            int(errors) == 0
            and int(processed) >= max(1, int(batch_size))
            and int(pending) > 0
        ):
            return 2.0
        return normal

    async def _thread_build_loop(self) -> None:
        from .claim_ledger import ClaimLedger
        from .prospective_memory import ProspectiveMemory
        from .thread_builder import ThreadBuilder
        from .thread_llm_arbiter import ThreadLLMArbiter
        from .thread_migration import ThreadMigration
        from .thread_projector import ThreadProjector

        try:
            if self._episodes is not None:
                migration = ThreadMigration(
                    self._episodes._connect,
                    self._episodes._lock,
                    self._episodes._threads,
                )
                try:
                    result = await self._run_background_work(
                        migration.run_scan,
                        read_only=self.thread_migration_read_only,
                        scope_id=str(self.character_name or "default"),
                        enqueue_existing=True,
                        batch_size=min(500, self.thread_worker_batch_size * 10),
                        max_batches=1,
                    )
                    logger.info("[memos-memory][thread][migration] startup scan done: %s", result)
                except Exception as exc:
                    logger.warning("[memos-memory][thread][migration] scan failed open: %s", exc)

            claims_due = 0.0
            claims_dirty = True
            maintenance_due = time.monotonic() + 120.0
            interval = max(60.0, float(self.thread_worker_interval_seconds))
            while True:
                interval = max(60.0, float(self.thread_worker_interval_seconds))
                sleep_seconds = interval
                if self._episodes is None:
                    await asyncio.sleep(interval)
                    continue
                if self._episodes.thread_is_paused():
                    await asyncio.sleep(interval)
                    continue
                try:
                    await self._run_background_work(
                        migration.run_scan, read_only=self.thread_migration_read_only,
                        scope_id=str(self.character_name or "default"), enqueue_existing=True,
                        batch_size=min(500, self.thread_worker_batch_size * 10), max_batches=1,
                    )
                    await self._run_background_work(self._thread_refresh_candidates)
                    builder = ThreadBuilder(
                        self._episodes._threads,
                        self._episodes._connect,
                        self._episodes._lock,
                        max_retries=self.thread_worker_max_retries,
                    )
                    result = await self._run_background_work(
                        builder.process_batch,
                        batch_size=self.thread_worker_batch_size,
                    )
                    if int(result.get("processed") or 0) > 0:
                        logger.info("[memos-memory][thread][builder] batch result: %s", result)
                    queue = self._episodes.thread_queue_counts()
                    sleep_seconds = self._thread_worker_next_delay(
                        interval,
                        processed=int(result.get("processed") or 0),
                        batch_size=self.thread_worker_batch_size,
                        errors=int(result.get("errors") or 0),
                        pending=int(queue.get("pending") or 0),
                    )

                    llm_result = {}
                    llm_processed = 0
                    if self.thread_llm_arbitration_enable:
                        provider = self._resolve_thread_llm_provider()
                        if provider is not None:

                            async def _call_thread_llm(prompt: str) -> str:
                                response = await self._plugin_llm_text_chat(
                                    provider,
                                    prompt=prompt,
                                    contexts=[],
                                    system_prompt="",
                                    timeout=self.thread_llm_timeout,
                                    label="thread_arbitration",
                                    optional=True,
                                    lane="background_state",
                                    task_family="thread_arbitration",
                                )
                                return str(getattr(response, "completion_text", "") or "")

                            llm_result = await ThreadLLMArbiter(
                                self._episodes._threads,
                                _call_thread_llm,
                                provider_id=self.thread_llm_provider_id,
                                model_id=_provider_display_name(provider),
                                timeout=self.thread_llm_timeout,
                                max_retries=self.thread_llm_max_retries,
                                daily_budget=self.thread_llm_daily_budget,
                                max_input_chars=self.thread_llm_max_input_chars,
                            ).process_pending(batch_size=self.thread_llm_batch_size)
                            llm_processed = int(llm_result.get("processed") or 0)
                            if llm_processed > 0:
                                logger.info("[memos-memory][thread][llm] batch result: %s", llm_result)

                    projected_count = 0
                    if self.thread_projection_enable:
                        projection = await self._run_background_work(
                            ThreadProjector(
                                self._episodes._threads,
                                min_confidence=self.thread_projection_min_confidence,
                            ).project,
                            scope_id=str(self.character_name or "default"),
                            episode_ids=list(dict.fromkeys([*[item["episode_id"] for item in result.get("episodes", [])], *llm_result.get("episode_ids", [])])),
                        )
                        projected_count = int(projection.get("threads_projected") or 0)
                        if projected_count > 0:
                            logger.info("[memos-memory][thread][projection] result: %s", projection)

                    claims_dirty = claims_dirty or bool(result.get("processed") or llm_processed or projected_count)
                    # Coalesce full ledger reconciliation; never repeat it for every batch.
                    if self.thread_claim_enable and claims_dirty and time.monotonic() >= claims_due:
                        claim_result = await self._run_background_work(
                            ClaimLedger(self._episodes._threads).rebuild_scope,
                            str(self.character_name or "default"),
                        )
                        claims_dirty = False
                        claims_due = time.monotonic() + 3600.0
                        logger.info("[memos-memory][thread][claims] refresh: %s", claim_result)
                        if self.thread_prospective_enable:
                            future_result = await self._run_background_work(
                                ProspectiveMemory(
                                self._episodes._threads,
                                str(getattr(self, "rp_time_timezone", "Asia/Shanghai")),
                            ).rebuild_scope,
                                str(self.character_name or "default"),
                            )
                            logger.info("[memos-memory][thread][prospective] refresh: %s", future_result)

                    if time.monotonic() >= maintenance_due:
                        current_hour = int(self._request_now().hour)
                        if current_hour != 23:
                            now_ts = time.time()
                            retention_days = max(
                                7,
                                min(3650, int(getattr(self, "thread_observation_retention_days", 180))),
                            )
                            maintenance = await self._run_background_work(
                                self._episodes._threads.maintenance,
                                observation_before=now_ts - retention_days * 86400,
                                rejected_before=now_ts - 30 * 86400,
                                limit=200,
                                analyze=True,
                                checkpoint=True,
                            )
                            if maintenance.get("deleted") or maintenance.get("skipped"):
                                logger.info(
                                    "[memos-memory][thread][maintenance] %s", maintenance
                                )
                        maintenance_due = time.monotonic() + 6 * 3600.0
                except Exception as exc:
                    logger.warning("[memos-memory][thread][builder] batch failed open: %s", exc)
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            # A cancelled await does not stop its SQLite worker. Lease recovery is
            # safe on the next batch; resetting running claims here is not.
            raise
        except Exception as exc:
            logger.warning("[memos-memory][thread] loop failed open: %s", exc)

    def _resolve_thread_llm_provider(self) -> Any | None:
        resolver = getattr(self, "_resolve_chat_provider", None)
        if callable(resolver):
            return resolver(self.thread_llm_provider_id or self.compress_provider_id)
        for provider_id in (self.thread_llm_provider_id, self.compress_provider_id):
            if not provider_id:
                continue
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception as exc:
                logger.warning(
                    "[memos-memory][thread][llm] provider '%s' unavailable: %s",
                    provider_id,
                    exc,
                )
                provider = None
            if provider is not None:
                return provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    @staticmethod
    def _thread_plan_payload(plan: Any, query: str, fallback: Any, context_text: str) -> dict[str, Any]:
        if isinstance(plan, dict):
            return dict(plan)
        if plan is not None:
            keys = (
                "intent", "search_text", "use_context", "context_used_reason",
                "thread_intent", "current_state_intent", "evolution_intent",
                "prospective_intent", "target_entities", "target_claim_slots",
                "time_constraints", "requires_counterevidence", "emotion_signal",
            )
            payload = {key: getattr(plan, key) for key in keys if hasattr(plan, key)}
            if payload:
                return payload
        return fallback.plan_for_search(query, context_text)

    async def _compose_thread_canary_for_request(
        self,
        *,
        query: str,
        context_text: str,
        selected_hits: list[dict[str, Any]],
        query_plan: Any,
        scope_id: str,
        session_id: str,
        request_id: str,
        current_time_valid: bool,
        base_memory_chars: int,
        preexisting_chars: int,
        now_ts: float,
        emotion_signal: float | None = None,
    ) -> dict[str, Any]:
        """Build a bounded Canary block from the already-frozen 5.1 result.

        This method must never call the mature recall path.  Any problem returns
        an empty text block and leaves the ProviderRequest byte-for-byte as it
        was before the caller invoked this method.
        """
        from .context_composer import ThreadContextComposer, stable_canary_decision

        empty = {"text": "", "metrics": {"outcome": "disabled"}}
        if not bool(getattr(self, "thread_memory_enable", False)):
            return empty
        episodes = getattr(self, "_episodes", None)
        store = getattr(episodes, "_threads", None) if episodes is not None else None
        if store is None:
            return {"text": "", "metrics": {"outcome": "fail_open", "reason": "thread_store_unavailable"}}

        decision = stable_canary_decision(
            scope_id=scope_id,
            session_id=session_id,
            seed=str(getattr(self, "thread_canary_seed", "memos-memory-6")),
            percent=int(getattr(self, "thread_canary_percent", 10)),
            mode=self._thread_effective_mode(),
            allowlist=getattr(self, "thread_canary_allowlist", ""),
            denylist=getattr(self, "thread_canary_denylist", ""),
        )
        from .thread_policy import POLICY_VERSION, fingerprint
        decision.update(policy_version=POLICY_VERSION, policy_fingerprint=fingerprint(self))
        started = time.perf_counter()
        plan = self._thread_plan_payload(query_plan, query, self._query_planner, context_text)
        emotion_signal = _resolve_emotion_signal(plan, emotion_signal)
        plan["emotion_signal"] = emotion_signal
        base_result = {
            "recall_architecture": "5.1_frozen_recall",
            "selected_count": len(selected_hits),
            "candidate_count": len(selected_hits),
            "hits": [{**dict(hit), "selected": True} for hit in selected_hits],
        }
        timeout_ms = max(50, min(3000, int(getattr(self, "thread_canary_timeout_ms", 300))))
        observation: dict[str, Any] = {
            "request_id": request_id,
            "scope_id": scope_id,
            "query_text": query,
            "evidence": [],
            "thread_ids": [],
            "subgraph": {},
            "claims": [],
            "prospective": [],
            "query_plan": plan,
            "routes": {},
            "dedup": {},
            "injection_preview": "",
            "latency_ms": 0.0,
        }

        async def record(metrics: dict[str, Any]) -> bool:
            observation["metrics"] = {**decision, **metrics}
            observation["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            try:
                await asyncio.wait_for(
                    self._run_background_work(
                        store.record_thread_query_observation, observation
                    ),
                    timeout=timeout_ms / 1000.0,
                )
                return True
            except Exception as exc:
                logger.warning("[memos-memory][thread][canary] observation failed open: %s", exc)
                return False

        if bool(getattr(self, "thread_canary_require_current_time", True)) and not current_time_valid:
            metrics = {"outcome": "fail_open", "reason": "current_time_missing", "injected": False}
            await record(metrics)
            return {"text": "", "metrics": {**decision, **metrics}}

        try:
            from .thread_retrieval import ThreadRetrievalLab

            result = await asyncio.wait_for(
                self._run_background_work(
                    ThreadRetrievalLab(
                        store, str(getattr(self, "rp_time_timezone", "Asia/Shanghai")),
                        **self._thread_node_limits()
                    ).run,
                    scope_id=scope_id,
                    query=query,
                    plan=plan,
                    base_result=base_result,
                    context_text=context_text,
                    emotion_signal=emotion_signal,
                    mode=(
                        "no_prospective"
                        if not bool(getattr(self, "thread_prospective_enable", True))
                        else "full" if selected_hits else "prospective_only"
                    ),
                    now_ts=now_ts,
                    record=False,
                ),
                timeout=timeout_ms / 1000.0,
            )
            composer = ThreadContextComposer(
                max_chars=int(getattr(self, "thread_canary_max_chars", 1200)),
                growth_percent=float(getattr(self, "thread_canary_growth_percent", 10.0)),
                timezone_name=str(getattr(self, "rp_time_timezone", "Asia/Shanghai")),
            )
            composed = composer.compose(
                result,
                plan=plan,
                base_memory_chars=base_memory_chars,
                preexisting_chars=preexisting_chars,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            metrics = {
                **composed.metrics,
                "selected_for_append": bool(decision["selected"] and composed.text),
                "injected": False,
                "append_confirmed": False,
                "append_status": (
                    "pending" if decision["selected"] and composed.text
                    else "shadow_preview" if composed.text else "empty"
                ),
                "would_inject": bool(composed.text),
                "base_selected_count": len(selected_hits),
                "base_recall_mutated": False,
                "extra_recall_calls": 0,
                "extra_llm_calls": 0,
                "elapsed_ms": elapsed_ms,
            }
            if elapsed_ms > timeout_ms:
                metrics.update({
                    "outcome": "fail_open", "reason": "latency_gate",
                    "selected_for_append": False, "injected": False,
                    "append_status": "latency_gate",
                })
            routes = result.get("routes") or {}
            fallback_composed = None
            prospective_route = routes.get("prospective") or {}
            if composed.text and prospective_route.get("selected"):
                without_prospective = {
                    **result,
                    "routes": {
                        **routes,
                        "prospective": {
                            **prospective_route,
                            "selected": None,
                        },
                    },
                }
                fallback_composed = composer.compose(
                    without_prospective,
                    plan=plan,
                    base_memory_chars=base_memory_chars,
                    preexisting_chars=preexisting_chars,
                )
            # The composer is the sole authority for both rendered text and the
            # structured objects that survived its relevance and budget gates.
            # Reconstructing this list from candidates would let truncated or
            # rejected rows leak into answer-consistency evaluation.
            visible_evidence = [dict(item) for item in composed.evidence]
            observation.update({
                "evidence": visible_evidence,
                "thread_ids": [str(item.get("thread_id") or "") for item in routes.get("threads") or [] if item.get("thread_id")],
                "subgraph": result.get("subgraph") or {},
                "claims": (result.get("routes") or {}).get("current_claims") or [],
                "prospective": [((result.get("routes") or {}).get("prospective") or {}).get("selected")]
                if ((result.get("routes") or {}).get("prospective") or {}).get("selected") else [],
                "routes": result.get("routes") or {},
                "dedup": result.get("dedup") or {},
                "injection_preview": composed.text,
            })
            if not await record(metrics):
                metrics.update({"outcome": "fail_open", "reason": "observation_write_failed", "injected": False})
                return {"text": "", "metrics": {**decision, **metrics}}
            text = composed.text if metrics["selected_for_append"] else ""
            surface_item_id = ""
            if metrics["selected_for_append"]:
                prospective = (result.get("routes") or {}).get("prospective") or {}
                selected_prospective = prospective.get("selected")
                if isinstance(selected_prospective, dict):
                    # The composer returned non-empty text only after its
                    # protected prospective section passed all gates and was
                    # rendered into the pending block.  Do not infer this from
                    # the candidate pool: only this marker reaches the append
                    # hook, while injected remains false until append confirms.
                    surface_item_id = str(selected_prospective.get("item_id") or "")
            logger.info(
                "[memos-memory][thread][canary] mode=%s selected=%s bucket=%d outcome=%s chars=%d latency=%.2fms",
                decision["mode"], decision["selected"], decision["bucket"], metrics.get("outcome"),
                len(text), elapsed_ms,
            )
            return {
                "text": text,
                "evidence": visible_evidence if text else [],
                "metrics": {**decision, **metrics},
                "prospective_surface_item_id": surface_item_id,
                "fallback_text": (
                    fallback_composed.text
                    if fallback_composed is not None
                    else ""
                ),
                "fallback_evidence": (
                    [dict(item) for item in fallback_composed.evidence]
                    if fallback_composed is not None
                    else []
                ),
            }
        except asyncio.TimeoutError:
            metrics = {"outcome": "fail_open", "reason": "timeout", "injected": False, "timeout_ms": timeout_ms}
            await record(metrics)
            return {"text": "", "metrics": {**decision, **metrics}}

    async def _finalize_thread_canary_observation(
        self,
        request_id: str,
        *,
        injected: bool,
        append_status: str,
        actual_text: str = "",
        actual_evidence: list[dict[str, Any]] | None = None,
    ) -> bool:
        episodes = getattr(self, "_episodes", None)
        store = getattr(episodes, "_threads", None) if episodes is not None else None
        if store is None or not request_id:
            return False
        timeout = max(0.05, min(1.0, float(getattr(self, "thread_canary_timeout_ms", 300)) / 1000.0))
        try:
            return bool(await asyncio.wait_for(
                self._run_background_work(
                    store.finalize_thread_query_observation,
                    request_id,
                    injected=bool(injected),
                    append_status=str(append_status or "unknown"),
                    actual_text=str(actual_text or ""),
                    actual_evidence=list(actual_evidence or []),
                ),
                timeout=timeout,
            ))
        except Exception as exc:
            logger.warning("[memos-memory][thread][canary] append confirmation failed open: %s", exc)
            return False
        except Exception as exc:
            metrics = {
                "outcome": "fail_open", "reason": "thread_pipeline_error",
                "error": str(exc)[:300], "injected": False,
            }
            await record(metrics)
            logger.warning("[memos-memory][thread][canary] failed open: %s", exc)
            return {"text": "", "metrics": {**decision, **metrics}}

    async def _run_thread_retrieval_lab(
        self,
        query: str,
        *,
        context_text: str = "",
        mode: str = "full",
        query_plan: Any = None,
        emotion_signal: float | None = None,
    ) -> dict[str, Any]:
        """Run the frozen 5.1 baseline and 6.x routes without mutating a request."""
        if not self.thread_retrieval_lab_enable:
            raise RuntimeError("thread retrieval laboratory is disabled")
        if not await self._ensure_init() or self._episodes is None or self._episodes._threads is None:
            raise RuntimeError("thread layer not ready")
        query = " ".join(str(query or "").split()).strip()
        if not query:
            raise ValueError("empty query")

        base_result = await self._eval_recall_query(query)
        plan = self._thread_plan_payload(query_plan, query, self._query_planner, context_text)
        emotion_signal = _resolve_emotion_signal(plan, emotion_signal)
        plan["emotion_signal"] = emotion_signal
        from .thread_retrieval import ThreadRetrievalLab

        result = await self._run_background_work(
            ThreadRetrievalLab(
                self._episodes._threads,
                str(getattr(self, "rp_time_timezone", "Asia/Shanghai")),
                **self._thread_node_limits(),
            ).run,
            scope_id=str(self.character_name or "default"),
            query=query,
            plan=plan,
            base_result=base_result,
            context_text=context_text,
            emotion_signal=emotion_signal,
            mode=mode,
            now_ts=self._request_now().timestamp(),
            record=True,
        )
        result["base"] = {
            "architecture": base_result.get("recall_architecture"),
            "selected_count": base_result.get("selected_count"),
            "candidate_count": base_result.get("candidate_count"),
            "hits": base_result.get("hits") or [],
            "injection_preview": base_result.get("injection_preview") or "",
        }
        result["integration"] = {
            "baseline": "5.1_frozen_recall",
            "access_route_mode": str(getattr(self, "memory_access_route_mode", "shadow")),
            "thread_mode": str(getattr(self, "thread_mode", "shadow")),
            "base_recall_mutated": False,
            "shared_llm_runtime": True,
        }
        return result

    async def _run_thread_eval(self, *, case_limit: int = 120) -> dict[str, Any]:
        if not await self._ensure_init() or self._episodes is None or self._episodes._threads is None:
            raise RuntimeError("thread layer not ready")
        from .thread_retrieval import ALGORITHM_VERSION, ThreadRetrievalLab

        lab = ThreadRetrievalLab(
            self._episodes._threads,
            str(getattr(self, "rp_time_timezone", "Asia/Shanghai")),
        )
        scope_id = str(self.character_name or "default")
        generated = await self._run_background_work(lab.generate_eval_cases, scope_id, case_limit)
        cases = self._episodes.thread_eval_cases(scope_id=scope_id, enabled_only=True, limit=case_limit)
        details: list[dict[str, Any]] = []
        passed = 0
        latency = 0.0
        baseline_hits = 0
        combined_hits = 0
        baseline_rr = 0.0
        combined_rr = 0.0
        temporal_total = 0
        temporal_passed = 0
        duplicate_count = 0
        injected_object_count = 0
        for case in cases:
            query = str(case.get("query") or "").strip()
            if not query:
                continue
            base = await self._eval_recall_query(query)
            plan = self._query_planner.plan_for_search(query, "")
            result = await self._run_background_work(
                lab.run,
                scope_id=scope_id,
                query=query,
                plan=plan,
                base_result=base,
                context_text="",
                mode="full",
                now_ts=self._request_now().timestamp(),
                record=False,
            )
            latency += float(result.get("elapsed_ms") or 0)
            score = await self._run_background_work(lab.score_eval_case, case, base, result)
            case_passed = bool(score["passed"])
            passed += int(case_passed)
            baseline_hits += int(score["baseline_hit"])
            combined_hits += int(score["combined_hit"])
            baseline_rr += float(score["baseline_rr"])
            combined_rr += float(score["combined_rr"])
            duplicate_count += int(score["duplicate_count"])
            injected_object_count += int(score["injected_object_count"])
            if str(case.get("case_type") or "") in {"evolution", "temporal_order"}:
                temporal_total += 1
                temporal_passed += int(score["order_ok"] and score["all_episodes_hit"])
            details.append({
                "case_id": case.get("case_id"),
                "case_type": case.get("case_type"),
                "query": query,
                "passed": case_passed,
                "expected": {
                    "claims": sorted(case.get("expected_claim_ids") or []),
                    "episodes": list(case.get("expected_episode_ids") or []),
                    "prospective": sorted(case.get("expected_prospective_ids") or []),
                    "order": list(case.get("expected_order") or []),
                },
                "score": score,
            })

        total = len(details)
        metrics = {
            "automatic_case_accuracy": round(passed / max(1, total), 4),
            "cases_total": total,
            "cases_passed": passed,
            "baseline_recall_at_k": round(baseline_hits / max(1, total), 4),
            "combined_recall_at_k": round(combined_hits / max(1, total), 4),
            "recall_delta": round((combined_hits - baseline_hits) / max(1, total), 4),
            "baseline_mrr": round(baseline_rr / max(1, total), 4),
            "combined_mrr": round(combined_rr / max(1, total), 4),
            "temporal_order_accuracy": round(temporal_passed / max(1, temporal_total), 4),
            "temporal_cases": temporal_total,
            "cross_layer_duplicate_rate": round(
                duplicate_count / max(1, injected_object_count), 6
            ),
            "avg_thread_latency_ms": round(latency / max(1, total), 2),
            "base_recall_mutated": False,
            "human_labels": 0,
            "label_quality": "deterministic_source_holdout",
            "access_baseline": "5.1",
        }
        run_id = self._episodes._threads.record_thread_eval_run(
            ALGORITHM_VERSION,
            {"case_limit": case_limit, "shadow": True, "access_baseline": "5.1"},
            {"metrics": metrics, "details": details},
            total,
            passed,
        )
        return {"run_id": run_id, "generated": generated, "metrics": metrics, "details": details}
