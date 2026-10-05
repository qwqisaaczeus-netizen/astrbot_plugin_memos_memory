from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .direct_llm import OpenAICompatibleTextClient, SecretValueStore, _atomic_json, validate_api_base_url
from .llm_compensation import TASK_FAMILIES
from .model_tasks import FAMILIES, task_key


_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


def migrate_external_model_state(legacy_dir: Path, stable_dir: Path) -> dict[str, Any]:
    """Copy rc7 runtime state into AstrBot's stable plugin_data directory.

    Existing canonical files always win. Legacy files are intentionally kept so
    an older package can still be rolled back without losing its settings.
    """
    legacy = Path(legacy_dir).expanduser().resolve()
    stable = Path(stable_dir).expanduser().resolve()
    result: dict[str, Any] = {"source": str(legacy), "target": str(stable), "copied": []}
    if legacy == stable:
        return result
    stable.mkdir(parents=True, exist_ok=True)

    source_json = legacy / "external_models.json"
    target_json = stable / "external_models.json"
    if source_json.is_file() and not target_json.exists():
        temp = target_json.with_suffix(target_json.suffix + ".migrating")
        shutil.copy2(source_json, temp)
        os.replace(temp, target_json)
        result["copied"].append("external_models.json")

    source_secrets = legacy / "external_model_secrets"
    target_secrets = stable / "external_model_secrets"
    if source_secrets.is_dir():
        target_secrets.mkdir(parents=True, exist_ok=True)
        for source in source_secrets.glob("*.json"):
            target = target_secrets / source.name
            if target.exists():
                continue
            temp = target.with_suffix(target.suffix + ".migrating")
            shutil.copy2(source, temp)
            os.replace(temp, target)
            result["copied"].append("external_model_secrets/" + source.name)

    source_db = legacy / "llm_compensation.db"
    target_db = stable / "llm_compensation.db"
    if source_db.is_file() and not target_db.exists():
        temp_db = target_db.with_suffix(".db.migrating")
        if temp_db.exists():
            temp_db.unlink()
        source_conn = sqlite3.connect(str(source_db), timeout=10)
        target_conn = sqlite3.connect(str(temp_db), timeout=10)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        os.replace(temp_db, target_db)
        result["copied"].append("llm_compensation.db")
    return result


def _bounded_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


class ExternalModelPoolProvider:
    """One plugin-provider facade with deadline-bounded model failover."""

    def __init__(self, registry: "ExternalModelRegistry", preferred_id: str = "") -> None:
        self.registry = registry
        self.preferred_id = str(preferred_id or "")
        self.provider_id = "external:" + (self.preferred_id or "default")

    def meta(self) -> Any:
        item = self.registry.model(self.preferred_id) or self.registry.default_model()
        label = str((item or {}).get("name") or (item or {}).get("model") or "external pool")
        return SimpleNamespace(id=self.provider_id, name=label, model=label)

    async def text_chat(
        self,
        *,
        prompt: str,
        contexts: list[Any] | None = None,
        system_prompt: str = "",
        timeout: float | None = None,
        max_tokens: int = 8192,
        request_max_retries: int | None = None,
        task_label: str = "",
    ) -> Any:
        return await self.registry.text_chat(
            self.preferred_id,
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
            timeout=timeout,
            max_tokens=max_tokens,
            request_max_retries=request_max_retries,
            task_label=task_label,
        )


class ExternalSingleProvider:
    """One selected API model for an Astr failure, independent of pool takeover."""

    def __init__(self, registry: "ExternalModelRegistry", model_id: str) -> None:
        self.registry = registry
        self.model_id = model_id
        self.provider_id = "external:" + model_id

    def meta(self) -> Any:
        item = self.registry.model(self.model_id) or {}
        name = str(item.get("name") or item.get("model") or self.model_id)
        return SimpleNamespace(id=self.provider_id, name=name, model=name)

    async def text_chat(self, *, prompt: str, contexts: list[Any] | None = None,
                        system_prompt: str = "", timeout: float | None = None,
                        max_tokens: int = 8192,
                        request_max_retries: int | None = None,
                        task_label: str = "") -> Any:
        return await self.registry.single_text_chat(
            self.model_id, prompt=prompt, contexts=contexts,
            system_prompt=system_prompt, timeout=timeout,
            max_tokens=max_tokens, task_label=task_label,
        )


class ExternalModelRegistry:
    """Persistent multi-model registry for plugin-owned OpenAI-compatible calls.

    Public JSON never contains API keys. Each key is stored in a separate
    user-scoped secret file and model edits are atomic.
    """

    def __init__(
        self,
        data_dir: Path,
        telemetry_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "external_models.json"
        self.secret_dir = self.data_dir / "external_model_secrets"
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self.enabled = False
        self.fallback_enabled = True
        self.default_model_id = ""
        self.astr_followup_enabled = False
        self.astr_followup_model_id = ""
        self.astr_followup_tasks: list[str] = []
        self.astr_followup_models: dict[str, str] = {}
        self.task_call_policies = {}
        self.astr_followup_timeout = 90.0
        self._models: list[dict[str, Any]] = []
        self._clients: dict[str, OpenAICompatibleTextClient] = {}
        self._providers: dict[str, ExternalModelPoolProvider] = {}
        self._retired_clients: list[OpenAICompatibleTextClient] = []
        self._health: dict[str, dict[str, Any]] = {}
        self._telemetry_callback = telemetry_callback
        self.load()

    def _secret(self, model_id: str) -> SecretValueStore:
        return SecretValueStore(self.secret_dir / f"{model_id}.json")

    @staticmethod
    def _new_id() -> str:
        return "model_" + secrets.token_hex(6)

    @staticmethod
    def _clean_id(value: Any) -> str:
        clean = str(value or "").strip()
        return clean if _ID_RE.fullmatch(clean) else ""

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            raw = {}
        models = raw.get("models") if isinstance(raw, dict) else []
        clean_models = []
        seen = set()
        for source in models if isinstance(models, list) else []:
            if not isinstance(source, dict):
                continue
            item = self._validate_model(source, require_key=False)
            if not item["id"] or item["id"] in seen:
                continue
            seen.add(item["id"])
            clean_models.append(item)
        with self._lock:
            self._models = clean_models
            self.enabled = bool(raw.get("enabled", False)) if isinstance(raw, dict) else False
            self.fallback_enabled = bool(raw.get("fallback_enabled", True)) if isinstance(raw, dict) else True
            self.default_model_id = self._clean_id(
                raw.get("default_model_id", "") if isinstance(raw, dict) else ""
            )
            self.astr_followup_enabled = bool(raw.get("astr_followup_enabled", False)) if isinstance(raw, dict) else False
            self.astr_followup_model_id = self._clean_id(raw.get("astr_followup_model_id", "")) if isinstance(raw, dict) else ""
            tasks = raw.get("astr_followup_tasks", []) if isinstance(raw, dict) else []
            self.astr_followup_tasks = sorted({
                task for task in tasks if isinstance(task, str) and task in TASK_FAMILIES
            }) if isinstance(tasks, list) else []
            self.astr_followup_timeout = _bounded_float(
                raw.get("astr_followup_timeout", 90) if isinstance(raw, dict) else 90,
                10.0, 300.0, 90.0,
            )
            from .call_policy import validate_overrides
            self.task_call_policies = validate_overrides(raw.get('task_call_policies', {}))
            mappings=raw.get('astr_followup_models',{}) if isinstance(raw,dict) else {}
            self.astr_followup_models={k:str(v) for k,v in mappings.items()
                if k in (set(FAMILIES)|TASK_FAMILIES) and isinstance(v,str)} if isinstance(mappings,dict) else {}
            if self.default_model_id not in {item["id"] for item in clean_models}:
                self.default_model_id = next((item["id"] for item in clean_models if item["enabled"]), "")
            self._reset_clients_locked()

    def _validate_model(self, source: dict[str, Any], *, require_key: bool) -> dict[str, Any]:
        model_id = self._clean_id(source.get("id")) or self._new_id()
        base_url = validate_api_base_url(source.get("base_url")) if source.get("base_url") else ""
        model = str(source.get("model") or "").strip()[:200]
        name = str(source.get("name") or model or model_id).strip()[:80]
        item = {
            "id": model_id,
            "name": name,
            "base_url": base_url,
            "model": model,
            "enabled": bool(source.get("enabled", True)),
            "priority": _bounded_int(source.get("priority", 50), 0, 999, 50),
            "max_retries": _bounded_int(source.get("max_retries", 0), 0, 1, 0),
            "timeout_seconds": _bounded_float(source.get("timeout_seconds", 60), 3.0, 600.0, 60.0),
            "temperature": _bounded_float(source.get("temperature", 0.1), 0.0, 2.0, 0.1),
            "max_output_tokens": _bounded_int(source.get("max_output_tokens", 16384), 256, 32768, 16384),
            "context_window_tokens": _bounded_int(source.get("context_window_tokens",
                (self.model(model_id) or {}).get('context_window_tokens', 0)), 0, 2097152, 0),
        }
        if require_key and item["enabled"] and not (base_url and model):
            raise ValueError(f"模型“{name}”启用时必须填写 Base URL 和 model")
        return item

    def _reset_clients_locked(self) -> None:
        self._retired_clients.extend(self._clients.values())
        self._clients = {}
        self._providers = {}

    def _write_locked(self) -> None:
        payload = {
            "version": 1,
            "enabled": self.enabled,
            "fallback_enabled": self.fallback_enabled,
            "default_model_id": self.default_model_id,
            "astr_followup_enabled": self.astr_followup_enabled,
            "astr_followup_model_id": self.astr_followup_model_id,
            "astr_followup_tasks": list(self.astr_followup_tasks),
            "astr_followup_models": dict(self.astr_followup_models),
            "task_call_policies": copy.deepcopy(self.task_call_policies),
            "astr_followup_timeout": self.astr_followup_timeout,
            "models": copy.deepcopy(self._models),
        }
        _atomic_json(self.path, payload)
        try:
            persisted = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            raise OSError("外置模型设置写入后无法重新读取") from exc
        for key in (
            "enabled", "fallback_enabled", "default_model_id",
            "astr_followup_enabled", "astr_followup_model_id",
            "astr_followup_tasks", "astr_followup_timeout", "astr_followup_models", "task_call_policies", "models",
        ):
            if persisted.get(key) != payload.get(key):
                raise OSError("外置模型设置持久化校验失败: " + key)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._save_lock:
            return self._save_serialized(payload)

    def _save_serialized(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("模型池设置必须是对象")
        incoming = payload.get("models")
        if not isinstance(incoming, list):
            raise ValueError("models 必须是数组")
        models = []
        pending_keys: dict[str, str | None] = {}
        seen = set()
        for raw in incoming[:32]:
            if not isinstance(raw, dict):
                raise ValueError("每个模型必须是对象")
            item = self._validate_model(raw, require_key=True)
            if item["id"] in seen:
                raise ValueError("模型 ID 重复: " + item["id"])
            seen.add(item["id"])
            key_text = str(raw.get("api_key") or "").strip()
            clear_key = bool(raw.get("clear_api_key"))
            if key_text:
                pending_keys[item["id"]] = key_text
            elif clear_key:
                pending_keys[item["id"]] = None
            models.append(item)
        enabled = bool(payload.get("enabled", False))
        default_id = self._clean_id(payload.get("default_model_id"))
        enabled_ids = [item["id"] for item in models if item["enabled"]]
        if default_id not in enabled_ids:
            default_id = enabled_ids[0] if enabled_ids else ""
        for item in models:
            next_key = pending_keys.get(item["id"], self._secret(item["id"]).load())
            if item["enabled"] and not next_key:
                raise ValueError(f"模型“{item['name']}”启用时必须配置 API Key")
        if enabled and not default_id:
            raise ValueError("启用外置模型池前至少需要一个已启用且配置完整的模型")
        followup_enabled = bool(payload.get("astr_followup_enabled", self.astr_followup_enabled))
        followup_model_id = self._clean_id(payload.get("astr_followup_model_id", self.astr_followup_model_id))
        tasks = payload.get("astr_followup_tasks", self.astr_followup_tasks)
        if not isinstance(tasks, list) or any(task not in TASK_FAMILIES for task in tasks):
            raise ValueError("跟接任务选择无效")
        mappings=payload.get('astr_followup_models',self.astr_followup_models)
        if not isinstance(mappings,dict) or any(k not in (set(FAMILIES)|TASK_FAMILIES) or
            not isinstance(v,str) or (v and v not in enabled_ids) for k,v in mappings.items()):
            raise ValueError('逐任务跟接模型无效或已停用')
        mappings=dict(mappings)
        from .call_policy import validate_overrides
        call_policies = validate_overrides(payload.get('task_call_policies', self.task_call_policies))
        if followup_enabled and not any(mappings.values()) and (followup_model_id not in enabled_ids or not tasks):
            raise ValueError("启用 Astr 跟接前须指定可用模型及至少一类任务")
        followup_timeout = _bounded_float(payload.get("astr_followup_timeout", self.astr_followup_timeout), 10, 300, 90)

        with self._lock:
            previous_models = copy.deepcopy(self._models)
            previous_enabled = self.enabled
            previous_fallback = self.fallback_enabled
            previous_default = self.default_model_id
            previous_followup = (
                self.astr_followup_enabled, self.astr_followup_model_id,
                list(self.astr_followup_tasks), self.astr_followup_timeout,
                dict(self.astr_followup_models),
                copy.deepcopy(self.task_call_policies),
            )
            previous_ids = {item["id"] for item in previous_models}
            next_ids = {item["id"] for item in models}
            affected_ids = previous_ids | next_ids
            previous_secrets = {
                model_id: self._secret(model_id).load()
                for model_id in affected_ids
            }
            self._models = models
            self.enabled = enabled
            self.fallback_enabled = bool(payload.get("fallback_enabled", True))
            self.default_model_id = default_id
            self.astr_followup_enabled = followup_enabled
            self.astr_followup_model_id = followup_model_id
            self.astr_followup_tasks = sorted(set(tasks))
            self.astr_followup_timeout = followup_timeout
            self.astr_followup_models = mappings
            self.task_call_policies = call_policies
            try:
                for model_id, value in pending_keys.items():
                    self._secret(model_id).save(value or "")
                for removed in previous_ids - next_ids:
                    self._secret(removed).save("")
                self._write_locked()
            except Exception:
                self._models = previous_models
                self.enabled = previous_enabled
                self.fallback_enabled = previous_fallback
                self.default_model_id = previous_default
                (self.astr_followup_enabled, self.astr_followup_model_id,
                 self.astr_followup_tasks, self.astr_followup_timeout,
                 self.astr_followup_models, self.task_call_policies) = previous_followup
                for model_id, value in previous_secrets.items():
                    try:
                        self._secret(model_id).save(value)
                    except Exception:
                        pass
                raise
            self._reset_clients_locked()
        return self.payload()

    def model(self, model_id: str) -> dict[str, Any] | None:
        clean = str(model_id or "").removeprefix("external:")
        with self._lock:
            for item in self._models:
                if item["id"] == clean:
                    return copy.deepcopy(item)
        return None

    def default_model(self) -> dict[str, Any] | None:
        return self.model(self.default_model_id)

    def _ordered_models(self, preferred_id: str = "") -> list[dict[str, Any]]:
        preferred = str(preferred_id or "").removeprefix("external:")
        with self._lock:
            active = [copy.deepcopy(item) for item in self._models if item["enabled"]]
            default_id = self.default_model_id
            fallback = self.fallback_enabled
        active.sort(key=lambda item: (int(item["priority"]), item["name"], item["id"]))
        first = preferred if preferred in {item["id"] for item in active} else default_id
        active.sort(key=lambda item: (0 if item["id"] == first else 1, int(item["priority"])))
        return active if fallback else active[:1]

    def _client(self, item: dict[str, Any]) -> OpenAICompatibleTextClient:
        model_id = item["id"]
        with self._lock:
            client = self._clients.get(model_id)
            if client is None:
                client = OpenAICompatibleTextClient("external-" + model_id)
                client.configure(
                    base_url=item["base_url"],
                    model=item["model"],
                    api_key=self._secret(model_id).load(),
                    max_retries=item["max_retries"],
                    temperature=item["temperature"],
                    timeout=item["timeout_seconds"],
                )
                self._clients[model_id] = client
            return client

    def provider(self, selection: str = "") -> ExternalModelPoolProvider | None:
        if not self.enabled or not self._ordered_models(selection):
            return None
        preferred = str(selection or "").removeprefix("external:")
        if not self.model(preferred):
            preferred = self.default_model_id
        with self._lock:
            if preferred not in self._providers:
                self._providers[preferred] = ExternalModelPoolProvider(self, preferred)
            return self._providers[preferred]

    def followup_provider(self, task: str) -> ExternalSingleProvider | None:
        with self._lock:
            key=task_key(task)
            family=FAMILIES.get(key,task)
            model_id = self.astr_followup_models.get(key,self.astr_followup_models.get(family,self.astr_followup_model_id))
            allowed = self.astr_followup_enabled and (key in self.astr_followup_models or
                family in self.astr_followup_models or family in self.astr_followup_tasks)
        item = self.model(model_id) if allowed else None
        if not item or not item["enabled"] or not self._secret(model_id).load():
            return None
        return ExternalSingleProvider(self, model_id)

    async def single_text_chat(self, model_id: str, *, prompt: str,
                               contexts: list[Any] | None, system_prompt: str,
                               timeout: float | None, max_tokens: int,
                               task_label: str = "") -> Any:
        item = self.model(model_id)
        if not item or not item["enabled"]:
            raise RuntimeError("跟接模型不存在或已停用")
        started = time.monotonic()
        try:
            result = await self._client(item).text_chat(
                prompt=prompt, contexts=contexts, system_prompt=system_prompt,
                timeout=float(item["timeout_seconds"] if timeout is None else timeout),
                max_tokens=min(int(max_tokens), int(item["max_output_tokens"])),
                request_max_retries=0,
            )
            self._mark(model_id, ok=True, elapsed_ms=round((time.monotonic() - started) * 1000))
            self._record_attempt(model_id, 0, started, "success")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark(model_id, ok=False, elapsed_ms=round((time.monotonic() - started) * 1000), error=str(exc))
            self._record_attempt(model_id, 0, started, type(exc).__name__)
            raise

    def provider_options(self, current: Any = "") -> list[dict[str, str]]:
        options = [{"value": "", "label": "外置模型池默认"}]
        with self._lock:
            models = [copy.deepcopy(item) for item in self._models if item["enabled"]]
            default_id = self.default_model_id
        models.sort(key=lambda item: (int(item["priority"]), item["name"]))
        for item in models:
            suffix = " · 默认" if item["id"] == default_id else ""
            options.append({
                "value": "external:" + item["id"],
                "label": f"{item['name']} · {item['model']}{suffix}",
            })
        configured = str(current or "")
        if configured.startswith("external:") and configured not in {item["value"] for item in options}:
            options.append({"value": configured, "label": configured + " · 当前不可用"})
        return options

    def payload(self) -> dict[str, Any]:
        from .call_policy import defaults
        with self._lock:
            models = copy.deepcopy(self._models)
            health = copy.deepcopy(self._health)
            enabled = self.enabled
            fallback = self.fallback_enabled
            default_id = self.default_model_id
            followup_enabled = self.astr_followup_enabled
            followup_model_id = self.astr_followup_model_id
            followup_tasks = list(self.astr_followup_tasks)
            followup_models = dict(self.astr_followup_models)
            call_policies = copy.deepcopy(self.task_call_policies)
            followup_timeout = self.astr_followup_timeout
        for item in models:
            item["has_api_key"] = bool(self._secret(item["id"]).load())
            item["api_key"] = ""
            item["health"] = health.get(item["id"], {})
        return {
            "enabled": enabled,
            "fallback_enabled": fallback,
            "default_model_id": default_id,
            "astr_followup_enabled": followup_enabled,
            "astr_followup_model_id": followup_model_id,
            "astr_followup_tasks": followup_tasks,
            "astr_followup_models": followup_models,
            "task_call_policies": call_policies,
            "task_call_defaults": {key: defaults(key) for key in FAMILIES},
            "astr_followup_timeout": followup_timeout,
            "models": models,
            "configured_count": sum(1 for item in models if item["enabled"] and item["has_api_key"]),
            "persisted": self.path.is_file(),
            "storage_path": str(self.path),
        }

    def _mark(self, model_id: str, *, ok: bool, elapsed_ms: int, error: str = "") -> None:
        with self._lock:
            state = self._health.setdefault(model_id, {
                "successes": 0, "failures": 0, "last_error": "",
                "last_latency_ms": 0, "last_success_at": 0.0, "last_failure_at": 0.0,
            })
            state["last_latency_ms"] = elapsed_ms
            if ok:
                state["successes"] += 1
                state["last_success_at"] = time.time()
                state["last_error"] = ""
            else:
                state["failures"] += 1
                state["last_failure_at"] = time.time()
                state["last_error"] = str(error or "unknown")[:240]

    async def text_chat(
        self,
        preferred_id: str,
        *,
        prompt: str,
        contexts: list[Any] | None,
        system_prompt: str,
        timeout: float | None,
        max_tokens: int,
        request_max_retries: int | None,
        task_label: str = "",
    ) -> Any:
        candidates = self._ordered_models(preferred_id)
        if not candidates:
            raise RuntimeError("外置模型池已启用，但没有可用模型")
        total_timeout = max(3.0, float(timeout or 120.0))
        deadline = time.monotonic() + total_timeout
        failures = []
        for index, item in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                break
            remaining_models = len(candidates) - index
            reserve = min(max(0, remaining_models - 1) * 3.0, remaining * 0.30)
            model_timeout = float(item["timeout_seconds"])
            if task_label.startswith(("episode_extract", "diary_render", "compress_llm")):
                # A saved 60s model timeout must not truncate a 120-150s
                # evidence/diary task before its own deadline.
                model_timeout = max(model_timeout, total_timeout)
            attempt_timeout = min(model_timeout, max(1.0, remaining - reserve))
            started = time.monotonic()
            try:
                result = await self._client(item).text_chat(
                    prompt=prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    timeout=attempt_timeout,
                    max_tokens=min(int(max_tokens), int(item["max_output_tokens"])),
                    request_max_retries=request_max_retries,
                )
                self._mark(item["id"], ok=True, elapsed_ms=round((time.monotonic() - started) * 1000))
                self._record_attempt(item["id"], index, started, "success")
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = round((time.monotonic() - started) * 1000)
                self._mark(item["id"], ok=False, elapsed_ms=elapsed, error=str(exc) or type(exc).__name__)
                self._record_attempt(item["id"], index, started, type(exc).__name__)
                failures.append(f"{item['name']}: {str(exc) or type(exc).__name__}")
        if deadline - time.monotonic() <= 0.1:
            raise asyncio.TimeoutError("外置模型池总调用时间已耗尽: " + " | ".join(failures[-3:]))
        raise RuntimeError("外置模型池全部失败: " + " | ".join(failures[-3:]))

    def _record_attempt(self, model_id: str, index: int, started: float, outcome: str) -> None:
        if self._telemetry_callback is None:
            return
        try:
            self._telemetry_callback({
                "provider": "external:" + model_id,
                "task": "external_model_attempt",
                "outcome": outcome,
                "fallback_index": index,
                "provider_ms": round((time.monotonic() - started) * 1000.0, 1),
                "ts": time.time(),
            })
        except Exception:
            pass

    async def test_model(self, model_id: str, timeout: float = 20.0) -> dict[str, Any]:
        item = self.model(model_id)
        if not item or not item["enabled"]:
            raise ValueError("模型不存在或未启用")
        started = time.monotonic()
        try:
            result = await self._client(item).test(timeout=min(45.0, max(3.0, float(timeout))))
            elapsed = round((time.monotonic() - started) * 1000)
            self._mark(item["id"], ok=True, elapsed_ms=elapsed)
            result.update({"model_id": item["id"], "name": item["name"]})
            return result
        except Exception as exc:
            self._mark(
                item["id"], ok=False,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                error=str(exc) or type(exc).__name__,
            )
            raise

    async def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values()) + list(self._retired_clients)
            self._clients = {}
            self._retired_clients = []
            self._providers = {}
        if clients:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
