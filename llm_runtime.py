from __future__ import annotations

import asyncio
import hashlib
import heapq
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .direct_llm import DirectLLMEmptyFinalError
from .generation_v2.adapters import dispatch_once


logger = logging.getLogger("astrbot")


class LLMCircuitOpenError(RuntimeError):
    """Raised when an optional plugin LLM task is cooling down."""


class LLMQueueTimeoutError(asyncio.TimeoutError):
    """Raised when a task cannot enter its provider lane before its queue budget."""


class LLMRuntimeClosedError(RuntimeError):
    """Raised when a late task reaches an unloading plugin instance."""


class LLMEmptyFinalError(RuntimeError):
    """The provider completed but supplied no final answer text."""


class LLMRouteExhaustedError(RuntimeError):
    """All permitted routes failed and a durable compensation record exists."""

    def __init__(self, message: str, *, failure_kind: str, record_id: str = "") -> None:
        self.failure_kind = str(failure_kind or "task_error")
        self.record_id = str(record_id or "")
        super().__init__(str(message or "LLM routes exhausted"))


@dataclass
class _Health:
    failures: int = 0
    circuit_until: float = 0.0
    last_error: str = ""
    last_error_kind: str = ""
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    skipped: int = 0


@dataclass(order=True)
class _GateWaiter:
    priority: int
    sequence: int
    future: asyncio.Future[Any] = field(compare=False)


class _ProviderGate:
    """A cancellable FIFO-with-priority gate for one AstrBot Provider."""

    def __init__(self, concurrency: int = 1) -> None:
        self._concurrency = max(1, int(concurrency))
        self._active = 0
        self._sequence = 0
        self._waiters: list[_GateWaiter] = []
        self._guard = asyncio.Lock()

    def snapshot(self) -> dict[str, int]:
        waiters = tuple(self._waiters)
        return {
            "active": self._active,
            "queued": sum(1 for item in waiters if not item.future.done()),
            "concurrency": self._concurrency,
        }

    def _grant_locked(self) -> None:
        while self._active < self._concurrency and self._waiters:
            waiter = heapq.heappop(self._waiters)
            if waiter.future.done():
                continue
            self._active += 1
            waiter.future.set_result(True)

    async def acquire(self, *, priority: int, timeout: float) -> None:
        if timeout <= 0:
            raise LLMQueueTimeoutError("provider queue deadline exhausted")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._guard:
            self._sequence += 1
            heapq.heappush(
                self._waiters,
                _GateWaiter(int(priority), self._sequence, future),
            )
            self._grant_locked()
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            granted = future.done() and not future.cancelled() and future.exception() is None
            if not future.done():
                future.cancel()
            async with self._guard:
                if granted:
                    self._active = max(0, self._active - 1)
                self._grant_locked()
            raise LLMQueueTimeoutError(
                f"provider queue wait exceeded {timeout:.3f}s"
            ) from exc
        except asyncio.CancelledError:
            granted = future.done() and not future.cancelled() and future.exception() is None
            if not future.done():
                future.cancel()
            async with self._guard:
                if granted:
                    self._active = max(0, self._active - 1)
                self._grant_locked()
            raise

    async def release(self) -> None:
        async with self._guard:
            self._active = max(0, self._active - 1)
            self._grant_locked()

    async def close(self) -> None:
        async with self._guard:
            for waiter in self._waiters:
                if not waiter.future.done():
                    waiter.future.set_exception(LLMRuntimeClosedError("LLM runtime closed"))
            self._waiters.clear()


class PluginLLMRuntime:
    """Priority-aware AstrBot Provider scheduler for plugin-owned LLM tasks.

    Providers remain on AstrBot's active event loop. One total deadline covers
    foreground deferral, provider queueing and the actual provider call. Health
    is separated into provider transport and task-family domains, so one memory
    compression timeout cannot disable xinchao or time insight.
    """

    _LANES = {
        "interactive_optional": (0, 0.75),
        "memory_critical": (10, None),
        "background_state": (20, None),
        "background_creative": (30, None),
    }

    def __init__(
        self,
        *,
        provider_concurrency: int = 2,
        external_concurrency: int = 3,
        interactive_queue_timeout: float = 0.75,
        foreground_lease_seconds: float = 240.0,
        defer_background_during_foreground: bool = True,
        telemetry_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider_concurrency = max(1, min(6, int(provider_concurrency)))
        self.external_concurrency = max(1, min(10, int(external_concurrency)))
        self.interactive_queue_timeout = max(
            0.01, min(3.0, float(interactive_queue_timeout))
        )
        self.foreground_lease_seconds = max(
            30.0, min(900.0, float(foreground_lease_seconds))
        )
        self.defer_background_during_foreground = bool(
            defer_background_during_foreground
        )
        self._provider_health: dict[str, _Health] = {}
        self._family_health: dict[tuple[str, str], _Health] = {}
        self._gates: dict[str, _ProviderGate] = {}
        self._foreground: dict[str, dict[str, float]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._telemetry: dict[str, dict[str, Any]] = {}
        self._telemetry_callback = telemetry_callback

    @staticmethod
    def provider_key(provider: Any) -> str:
        try:
            meta = provider.meta()
            value = getattr(meta, "id", "") or getattr(meta, "name", "")
            if value:
                return str(value)
        except Exception:
            pass
        return str(
            getattr(provider, "provider_id", "")
            or getattr(provider, "id", "")
            or provider.__class__.__name__
        )

    @staticmethod
    def _supports_keyword(method: Any, name: str) -> bool:
        try:
            params = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        # Astr providers accept **kwargs but some silently discard them.
        return any(item.name == name for item in params)

    @classmethod
    def _supports_request_max_retries(cls, method: Any) -> bool:
        return cls._supports_keyword(method, "request_max_retries")

    @staticmethod
    def _task_family(label: str) -> str:
        value = str(label or "plugin_llm").lower()
        if value.startswith("xinchao_live"):
            return "xinchao_live"
        if value.startswith("xinchao_post"):
            return "xinchao_post"
        if any(value.startswith(prefix) for prefix in (
            "xinchao_dream", "xinchao_proactive", "xinchao_daytime",
        )):
            return "creative"
        if value.startswith("xinchao"):
            return "xinchao_other"
        if value.startswith("time_insight"):
            return "time_insight"
        if value.startswith("profile"):
            return "profile"
        if value.startswith("semantic_state"):
            return "semantic_state"
        if value.startswith("dream") or value.startswith("proactive"):
            return "creative"
        if any(token in value for token in (
            "compress", "episode_extract", "diary_render", "memory_generation",
        )):
            return "memory_generation"
        if value.startswith("query_plan"):
            return "query_plan"
        if value.startswith("webui"):
            return "webui_probe"
        return value[:64] or "plugin_llm"

    @classmethod
    def _infer_lane(cls, label: str, optional: bool) -> str:
        family = cls._task_family(label)
        if family in {"xinchao_live", "query_plan", "webui_probe"}:
            return "interactive_optional"
        if family == "memory_generation" or not optional:
            return "memory_critical"
        if family == "creative":
            return "background_creative"
        return "background_state"

    @staticmethod
    def _error_kind(exc: BaseException) -> str:
        if isinstance(exc, LLMQueueTimeoutError):
            return "queue_timeout"
        if isinstance(exc, LLMEmptyFinalError):
            return "empty_final"
        if isinstance(exc, DirectLLMEmptyFinalError):
            return "empty_final"
        text = f"{exc.__class__.__name__}: {exc}".lower()
        if isinstance(exc, asyncio.TimeoutError) or "timeout" in text or "timed out" in text:
            return "timeout"
        if any(word in text for word in (
            "401", "403", "unauthorized", "forbidden", "api key", "authentication",
        )):
            return "auth"
        if any(word in text for word in ("429", "rate limit", "quota", "too many requests")):
            return "rate_limit"
        if any(word in text for word in (
            "500", "502", "503", "504", "529", "connection", "connecterror",
            "network", "no available", "unavailable", "no channel", "model not found",
        )):
            return "transport"
        return "task_error"

    @staticmethod
    def _family_failure_policy(kind: str) -> tuple[int, float]:
        if kind == "timeout":
            return 2, 60.0
        if kind == "rate_limit":
            return 1, 90.0
        if kind == "transport":
            return 2, 60.0
        return 2, 60.0

    @staticmethod
    def _provider_failure_policy(kind: str) -> tuple[int, float] | None:
        if kind == "auth":
            return 1, 1800.0
        if kind == "rate_limit":
            return 2, 180.0
        if kind == "transport":
            return 2, 120.0
        return None

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._closed:
            raise LLMRuntimeClosedError("LLM runtime closed")
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(
                "plugin LLM Provider call attempted from a non-AstrBot event loop"
            )
        return loop

    def _provider_state(self, key: str) -> _Health:
        return self._provider_health.setdefault(key, _Health())

    def _family_state(self, key: str, family: str) -> _Health:
        return self._family_health.setdefault((key, family), _Health())

    @staticmethod
    def _gate_key(key: str) -> str:
        # Pool facades may fail over to the same endpoint; cap all plugin-owned
        # independent API traffic together, including Xinchao's direct client.
        return "__external_api__" if key.startswith(("external:", "direct:")) else key

    def _gate(self, key: str) -> _ProviderGate:
        gate_key = self._gate_key(key)
        return self._gates.setdefault(
            gate_key,
            _ProviderGate(concurrency=(
                self.external_concurrency if gate_key == "__external_api__"
                else self.provider_concurrency
            )),
        )

    def _record_telemetry(self, key: str, sample: dict[str, Any]) -> None:
        self._telemetry[key] = sample
        if self._telemetry_callback is not None:
            try:
                self._telemetry_callback({"provider": key, **sample})
            except Exception:
                logger.exception("[memos-memory][llm] telemetry callback failed")

    @staticmethod
    def _health_state(item: _Health, now: float) -> str:
        if item.circuit_until > now:
            return "circuit_open"
        if item.failures:
            return "degraded"
        if item.last_success_at:
            return "healthy"
        return "idle"

    @staticmethod
    def _health_payload(item: _Health, now: float) -> dict[str, Any]:
        return {
            "state": PluginLLMRuntime._health_state(item, now),
            "failures": item.failures,
            "cooldown_remaining_seconds": round(
                max(0.0, item.circuit_until - now), 1
            ),
            "last_error": item.last_error,
            "last_error_kind": item.last_error_kind,
            "last_failure_at": item.last_failure_at,
            "last_success_at": item.last_success_at,
            "skipped": item.skipped,
        }

    def _clean_foreground(self, key: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        scopes = self._foreground.get(key)
        if not scopes:
            return
        for scope, expiry in list(scopes.items()):
            if expiry <= current:
                scopes.pop(scope, None)
        if not scopes:
            self._foreground.pop(key, None)

    def begin_foreground(self, provider: Any, scope: str) -> None:
        if provider is None or self._closed:
            return
        key = self.provider_key(provider)
        scope = str(scope or "global")
        self._foreground.setdefault(key, {})[scope] = (
            time.monotonic() + self.foreground_lease_seconds
        )

    def end_foreground(self, provider: Any | None = None, scope: str = "") -> None:
        scope = str(scope or "")
        keys = [self.provider_key(provider)] if provider is not None else list(self._foreground)
        for key in keys:
            scopes = self._foreground.get(key)
            if not scopes:
                continue
            if scope:
                scopes.pop(scope, None)
            else:
                scopes.clear()
            if not scopes:
                self._foreground.pop(key, None)

    def foreground_busy(self, provider: Any) -> bool:
        key = self.provider_key(provider)
        self._clean_foreground(key)
        return bool(self._foreground.get(key))

    async def _wait_foreground_clear(self, key: str, deadline: float) -> float:
        started = time.monotonic()
        if not self.defer_background_during_foreground:
            return 0.0
        while True:
            now = time.monotonic()
            self._clean_foreground(key, now)
            if not self._foreground.get(key):
                return max(0.0, time.monotonic() - started)
            remaining = deadline - now
            if remaining <= 0:
                raise LLMQueueTimeoutError(
                    "total deadline exhausted while AstrBot foreground response was active"
                )
            await asyncio.sleep(min(0.05, remaining))

    def _check_circuit(self, key: str, family: str) -> None:
        now = time.monotonic()
        provider_state = self._provider_state(key)
        family_state = self._family_state(key, family)
        blocked: tuple[str, _Health] | None = None
        if provider_state.circuit_until > now:
            blocked = ("provider", provider_state)
        elif family_state.circuit_until > now:
            blocked = ("task", family_state)
        if blocked is None:
            return
        domain, state = blocked
        state.skipped += 1
        remaining = max(0.0, state.circuit_until - now)
        raise LLMCircuitOpenError(
            f"{domain} circuit for {key}/{family} cooling down {remaining:.1f}s: "
            f"{state.last_error}"
        )

    def _mark_failure(
        self,
        key: str,
        family: str,
        label: str,
        exc: BaseException,
        *,
        optional: bool,
    ) -> None:
        now_wall = time.time()
        now_mono = time.monotonic()
        kind = self._error_kind(exc)
        message = str(exc)[:240] or exc.__class__.__name__

        family_state = self._family_state(key, family)
        if kind == "queue_timeout":
            # A full provider lane is scheduling pressure, not evidence that
            # this task family or the provider transport is unhealthy. Keep it
            # visible without suppressing the next interactive assessment.
            family_state.last_error = message
            family_state.last_error_kind = kind
            family_state.last_failure_at = now_wall
            family_state.skipped += 1
            logger.warning(
                "[memos-memory][llm] provider=%s family=%s task=%s "
                "failure=queue_timeout provider_circuit=False task_circuit=False error=%s",
                key,
                family,
                label,
                message,
            )
            return
        family_state.failures += 1
        family_state.last_error = message
        family_state.last_error_kind = kind
        family_state.last_failure_at = now_wall
        if optional and family not in {"xinchao_live", "xinchao_post"}:
            threshold, cooldown = self._family_failure_policy(kind)
            if family_state.failures >= threshold:
                family_state.circuit_until = max(
                    family_state.circuit_until, now_mono + cooldown
                )

        provider_state = self._provider_state(key)
        provider_state.last_error = message
        provider_state.last_error_kind = kind
        provider_state.last_failure_at = now_wall
        provider_policy = self._provider_failure_policy(kind)
        if provider_policy is not None:
            provider_state.failures += 1
            threshold, cooldown = provider_policy
            if provider_state.failures >= threshold:
                provider_state.circuit_until = max(
                    provider_state.circuit_until, now_mono + cooldown
                )

        logger.warning(
            "[memos-memory][llm] provider=%s family=%s task=%s failure=%s "
            "provider_circuit=%s task_circuit=%s error=%s",
            key,
            family,
            label,
            kind,
            provider_state.circuit_until > now_mono,
            family_state.circuit_until > now_mono,
            message,
        )

    def _mark_success(self, key: str, family: str) -> None:
        now = time.time()
        family_state = self._family_state(key, family)
        family_state.failures = 0
        family_state.circuit_until = 0.0
        family_state.last_error = ""
        family_state.last_error_kind = ""
        family_state.last_success_at = now

        provider_state = self._provider_state(key)
        provider_state.failures = 0
        provider_state.circuit_until = 0.0
        provider_state.last_error = ""
        provider_state.last_error_kind = ""
        provider_state.last_success_at = now

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        output: dict[str, dict[str, Any]] = {}
        # WebUI reads this from its HTTP thread while mutations happen only on
        # AstrBot's event loop. Snapshot the mappings first so inspection never
        # iterates a live dictionary during a provider completion callback.
        provider_health = dict(self._provider_health)
        family_health = dict(self._family_health)
        gates = dict(self._gates)
        foreground = {
            key: dict(scopes) for key, scopes in tuple(self._foreground.items())
        }
        telemetry = {
            key: dict(value) for key, value in tuple(self._telemetry.items())
        }
        keys = set(provider_health) | (set(gates) - {"__external_api__"}) | {
            key for key, _family in family_health
        }
        for key in sorted(keys):
            provider = self._health_payload(provider_health.get(key, _Health()), now)
            families = {
                family: self._health_payload(item, now)
                for (provider_key, family), item in family_health.items()
                if provider_key == key
            }
            active_scopes = sum(
                1 for expires_at in (foreground.get(key) or {}).values()
                if float(expires_at or 0.0) > now
            )
            provider.update({
                "families": families,
                "gate": gates[self._gate_key(key)].snapshot() if self._gate_key(key) in gates else {
                    "active": 0, "queued": 0, "concurrency": self.provider_concurrency,
                },
                "foreground_scopes": active_scopes,
                "last_task": telemetry.get(key) or {},
            })
            output[key] = provider
        return output

    async def call(
        self,
        provider: Any,
        *,
        prompt: str,
        contexts: list[Any] | None = None,
        system_prompt: str = "",
        timeout: float | None = None,
        label: str = "plugin_llm",
        optional: bool = False,
        lane: str = "",
        task_family: str = "",
        queue_timeout: float | None = None,
        request_max_retries: int | None = None,
        call_policy: dict | None = None,
    ) -> Any:
        self._bind_loop()
        key = self.provider_key(provider)
        family = str(task_family or self._task_family(label))
        lane = str(lane or self._infer_lane(label, optional))
        if lane not in self._LANES:
            lane = "background_state" if optional else "memory_critical"
        priority, lane_queue_timeout = self._LANES[lane]
        if lane == "interactive_optional" and queue_timeout is None:
            lane_queue_timeout = self.interactive_queue_timeout
        elif queue_timeout is not None:
            lane_queue_timeout = max(0.01, float(queue_timeout))

        total_timeout = max(0.01, float(timeout or 120.0))
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]
        started = time.monotonic()
        deadline = started + total_timeout
        foreground_wait = 0.0
        foreground_started: float | None = None
        queue_started: float | None = None
        queue_wait = 0.0
        provider_started: float | None = None
        finish_reason = ""
        completion_tokens: int | None = None
        reasoning_tokens: int | None = None
        gate = self._gate(key)
        acquired = False
        try:
            if optional:
                self._check_circuit(key, family)
            if lane in {"background_state", "background_creative"}:
                foreground_started = time.monotonic()
                foreground_wait = await self._wait_foreground_clear(key, deadline)

            queue_started = time.monotonic()
            remaining_total = deadline - queue_started
            queue_budget = remaining_total
            if lane_queue_timeout is not None:
                queue_budget = min(queue_budget, float(lane_queue_timeout))
            await gate.acquire(priority=priority, timeout=queue_budget)
            acquired = True
            queue_wait = max(0.0, time.monotonic() - queue_started)
            if optional:
                self._check_circuit(key, family)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMQueueTimeoutError("total deadline exhausted before provider call")

            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "contexts": contexts or [],
                "system_prompt": system_prompt,
            }
            if self._supports_request_max_retries(provider.text_chat):
                kwargs["request_max_retries"] = (
                    1 if request_max_retries is None
                    else max(0, min(1, int(request_max_retries)))
                )
            # The patched Astr OpenAI provider uses request_timeout for its
            # SDK request. Older providers still obey the outer deadline.
            if self._supports_keyword(provider.text_chat, "request_timeout"):
                kwargs["request_timeout"] = remaining
            elif self._supports_keyword(provider.text_chat, "timeout"):
                kwargs["timeout"] = remaining
            if key.startswith(("external:", "direct:")):
                if self._supports_keyword(provider.text_chat, "task_label"):
                    kwargs["task_label"] = label
                if self._supports_keyword(provider.text_chat, "max_tokens"):
                    kwargs["max_tokens"] = (
                        16384 if label.startswith("diary_render")
                        else 8192 if family == "memory_generation"
                        else 8192 if family == "xinchao_post"
                        else 4096 if family == "xinchao_live"
                        else 2048 if family == "time_insight" or label.startswith("xinchao")
                        else 8192
                    )

            provider_started = time.monotonic()
            if call_policy is not None:
                from .generation_v2.transport import invoke
                result = await invoke(provider, {'prompt': prompt, 'contexts': contexts or [],
                    'system_prompt': system_prompt}, remaining, call_policy)
            else:
                result = await dispatch_once(provider, kwargs, remaining)
            provider_ms = (time.monotonic() - provider_started) * 1000.0
            total_ms = (time.monotonic() - started) * 1000.0
            output_text = str(getattr(result, "completion_text", "") or "")
            output_chars = len(output_text)
            raw_completion = getattr(result, "raw_completion", None)
            choices = getattr(raw_completion, "choices", None) or []
            finish_reason = str(getattr(choices[0], "finish_reason", "") or "") if choices else ""
            raw_usage = getattr(raw_completion, "usage", None)
            completion_tokens = getattr(raw_usage, "completion_tokens", None)
            details = getattr(raw_usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            if not output_text.strip():
                raise LLMEmptyFinalError(
                    "provider returned no final text "
                    f"finish_reason={finish_reason or 'unknown'} "
                    f"completion_tokens={completion_tokens} "
                    f"reasoning_tokens={reasoning_tokens}"
                )
            self._mark_success(key, family)
            self._record_telemetry(key, {
                "task": label,
                "family": family,
                "lane": lane,
                "outcome": "success",
                "foreground_wait_ms": round(foreground_wait * 1000.0, 1),
                "queue_wait_ms": round(queue_wait * 1000.0, 1),
                "provider_ms": round(provider_ms, 1),
                "total_ms": round(total_ms, 1),
                "prompt_chars": len(prompt),
                "prompt_fingerprint": prompt_fingerprint,
                "output_chars": output_chars,
                "finish_reason": finish_reason,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "deadline_seconds": round(total_timeout, 1),
                "output_limit": kwargs.get("max_tokens"),
                "sdk_timeout_enabled": "request_timeout" in kwargs or "timeout" in kwargs,
                "request_max_retries": kwargs.get("request_max_retries"),
                "ts": time.time(),
            })
            logger.info(
                "[memos-memory][llm] task=%s provider=%s lane=%s queue=%.1fms "
                "call=%.1fms total=%.1fms",
                label, key, lane, queue_wait * 1000.0, provider_ms, total_ms,
            )
            return result
        except asyncio.CancelledError:
            raise
        except LLMCircuitOpenError as exc:
            self._record_telemetry(key, {
                "task": label, "family": family, "lane": lane,
                "outcome": "circuit_open", "error_type": type(exc).__name__,
                "foreground_wait_ms": round(foreground_wait * 1000.0, 1),
                "queue_wait_ms": round(queue_wait * 1000.0, 1),
                "provider_ms": 0.0,
                "total_ms": round((time.monotonic() - started) * 1000.0, 1),
                "ts": time.time(),
            })
            raise
        except Exception as exc:
            self._mark_failure(key, family, label, exc, optional=optional)
            if isinstance(exc, DirectLLMEmptyFinalError):
                finish_reason = exc.finish_reason
                completion_tokens = exc.completion_tokens
                reasoning_tokens = exc.reasoning_tokens
            now = time.monotonic()
            if foreground_started is not None and queue_started is None:
                foreground_wait = now - foreground_started
            if queue_started is not None and not acquired:
                queue_wait = now - queue_started
            self._record_telemetry(key, {
                "task": label,
                "family": family,
                "lane": lane,
                "outcome": self._error_kind(exc),
                "error_type": type(exc).__name__,
                "foreground_wait_ms": round(foreground_wait * 1000.0, 1),
                "queue_wait_ms": round(queue_wait * 1000.0, 1),
                "provider_ms": round(max(0.0, now - provider_started) * 1000.0, 1)
                if provider_started is not None else 0.0,
                "total_ms": round((now - started) * 1000.0, 1),
                "prompt_chars": len(prompt),
                "prompt_fingerprint": prompt_fingerprint,
                "deadline_seconds": round(total_timeout, 1),
                "output_limit": kwargs.get("max_tokens") if provider_started is not None else None,
                "finish_reason": finish_reason,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "sdk_timeout_enabled": (
                    "request_timeout" in kwargs or "timeout" in kwargs
                ) if provider_started is not None else False,
                "ts": time.time(),
            })
            logger.warning(
                "[memos-memory][llm] task=%s provider=%s outcome=%s queue=%.1fms "
                "call=%.1fms total=%.1fms prompt_chars=%d deadline=%.1fs",
                label, key, self._error_kind(exc), queue_wait * 1000.0,
                max(0.0, now - provider_started) * 1000.0 if provider_started is not None else 0.0,
                (now - started) * 1000.0, len(prompt), total_timeout,
            )
            raise
        finally:
            if acquired:
                await gate.release()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for gate in list(self._gates.values()):
            await gate.close()
        self._foreground.clear()
