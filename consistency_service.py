"""Bounded request/response consistency observation worker.

The worker owns only immutable dictionaries.  Local checks run off the request
and response hooks; an optional coroutine callback is dispatched back to the
AstrBot event loop rather than executed in this worker thread.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import queue
import secrets
import threading
import time
from typing import Any, Callable

from .consistency_guard import evaluate

logger = logging.getLogger(__name__)


class ConsistencyService:
    def __init__(self, store: Any, evaluator: Callable[[dict[str, Any]], Any] | None = None,
                 *, capacity: int = 64, timeout: float = 0.25, mode: str = "shadow",
                 ttl: float = 900.0, llm_enable: bool = False,
                 llm_callback: Callable[[dict[str, Any], list[dict[str, Any]]], Any] | None = None,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.store = store
        self.evaluator = evaluator or evaluate
        normalized = str(mode or "shadow").strip().lower()
        self.mode = normalized if normalized in {"off", "shadow"} else "shadow"
        self.capacity = max(1, min(4096, int(capacity or 64)))
        self.timeout = max(0.01, min(30.0, float(timeout or .25)))
        self.ttl = max(1.0, min(86400.0, float(ttl or 900.0)))
        self.llm_enable = bool(llm_enable)
        self.llm_callback = llm_callback
        self.loop = loop
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=self.capacity)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._closed = False
        self._seen: dict[tuple[str, str], float] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._terminal: dict[str, float] = {}
        self.dropped = {"queue_full": 0, "pending_full": 0, "expired": 0, "invalid": 0,
                        "closed": 0, "late": 0, "timeout": 0, "errors": 0}
        self._worker = threading.Thread(target=self._run, name="memos-consistency", daemon=True)
        self._worker.start()

    def _record_unpaired(self, request_id: str, pair: dict[str, Any], reason: str) -> None:
        """Persist an incomplete pair as skipped instead of silently losing it."""
        try:
            snapshot = pair.get("snapshot") or {}
            response = pair.get("response") or {}
            answer = str(response.get("answer") or "")
            fields = {
                "observation_status": "skipped",
                "skip_reason": reason,
                "consistency_status": "skipped",
                "terminal": 1,
            }
            if snapshot:
                fields.update({
                    "scope_id": str(snapshot.get("scope_id") or "default"),
                    "query_text": str(snapshot.get("query_text") or snapshot.get("query") or "")[:1000],
                    "snapshot_complete": int(bool(snapshot.get("snapshot_complete"))),
                    "thread_used": int(bool(snapshot.get("thread_used"))),
                })
            if response:
                fields.update({
                    "response_status": str(response.get("response_status") or "")[:40],
                    "chunk_status": str(response.get("chunk_status") or "")[:40],
                    "answer_hash": hashlib.sha256(answer.encode("utf-8")).hexdigest() if answer else "",
                    "answer_chars": len(answer),
                    "answer_preview": answer[:240],
                })
            with self._lock:
                self._terminal[request_id] = time.time()
            recorder = getattr(self.store, "thread_record_consistency_result", None)
            if recorder is not None:
                recorder(request_id, [], **fields)
            else:
                self.store.thread_record_request_observation(request_id, **fields)
        except Exception as exc:
            self.dropped["errors"] += 1
            logger.warning("[consistency] incomplete observation persistence failed: %s", type(exc).__name__)

    def _prune_locked(self, now: float) -> list[tuple[str, dict[str, Any], str]]:
        for key, stamp in list(self._seen.items()):
            if now - stamp > self.ttl:
                self._seen.pop(key, None)
        terminal_ttl = max(60.0, self.ttl * 4.0)
        for request_id, stamp in list(self._terminal.items()):
            if now - stamp > terminal_ttl:
                self._terminal.pop(request_id, None)
        expired: list[tuple[str, dict[str, Any], str]] = []
        for request_id, pair in list(self._pending.items()):
            if now - float(pair.get("_ts", now)) > self.ttl:
                self._pending.pop(request_id, None)
                missing = "missing_response" if "response" not in pair else "missing_snapshot"
                expired.append((request_id, pair, missing))
                self.dropped["expired"] += 1
        while len(self._pending) > self.capacity:
            oldest = min(self._pending, key=lambda key: float(self._pending[key].get("_ts", now)))
            pair = self._pending.pop(oldest, {})
            missing = "missing_response" if "response" not in pair else "missing_snapshot"
            expired.append((oldest, pair, missing))
            self.dropped["pending_full"] += 1
        return expired

    def _submit(self, kind: str, item: dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            self.dropped["invalid"] += 1
            return False
        request_id = str(item.get("request_id") or "").strip()
        if not request_id:
            self.dropped["invalid"] += 1
            return False
        now = time.time()
        with self._lock:
            if request_id in self._terminal:
                self.dropped["late"] += 1
                return False
            if self._closed:
                self.dropped["closed"] += 1
                return False
            key = (kind, request_id)
            if key in self._seen:
                return True
            try:
                self._queue.put_nowait((kind, dict(item)))
            except queue.Full:
                self.dropped["queue_full"] += 1
                return False
            self._seen[key] = now
            return True

    def submit_snapshot(self, snapshot: dict[str, Any]) -> bool:
        with self._lock:
            if self._closed:
                self.dropped["closed"] += 1
                return False
        if not isinstance(snapshot, dict):
            self.dropped["invalid"] += 1
            return False
        if snapshot.get("snapshot_complete") is False:
            self.dropped["invalid"] += 1
            request_id = str(snapshot.get("request_id") or "").strip()
            if request_id:
                self._record_unpaired(request_id, {"snapshot": dict(snapshot)}, "incomplete_snapshot")
            return False
        try:
            normalized = copy.deepcopy(snapshot)
            refs = normalized.get("references", normalized.get("evidence", []))
            if isinstance(refs, dict):
                valid = all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                            for rows in refs.values())
            else:
                valid = isinstance(refs, list) and all(isinstance(row, dict) for row in refs)
            if not valid:
                raise ValueError("invalid evidence shape")
            encoded = json.dumps(refs, ensure_ascii=False, allow_nan=False)
            normalized.setdefault("evidence_json", encoded)
        except (TypeError, ValueError, RecursionError):
            # Queue a terminal skip, preserving the request identity without
            # pretending corrupt evidence is a successfully checked empty set.
            self.dropped["invalid"] += 1
            return self._submit("invalid_snapshot", {
                "request_id": str(snapshot.get("request_id") or ""),
                "query_text": str(snapshot.get("query_text") or snapshot.get("query") or "")[:1000],
                "snapshot_complete": False,
            })
        normalized.setdefault("snapshot_complete", True)
        if "query_text" not in normalized and "query" in normalized:
            normalized["query_text"] = normalized.get("query") or ""
        return self._submit("snapshot", normalized)

    def submit_response(self, response: dict[str, Any]) -> bool:
        if not isinstance(response, dict):
            self.dropped["invalid"] += 1
            return False
        return self._submit("response", response)

    def submit_observation(self, observation: dict[str, Any]) -> bool:
        """Queue a request-row update without performing SQLite I/O in a hook."""
        if not isinstance(observation, dict):
            self.dropped["invalid"] += 1
            return False
        request_id = str(observation.get("request_id") or "").strip()
        if not request_id:
            self.dropped["invalid"] += 1
            return False
        with self._lock:
            if self._closed:
                self.dropped["closed"] += 1
                return False
            try:
                self._queue.put_nowait(("observation", dict(observation)))
            except queue.Full:
                self.dropped["queue_full"] += 1
                return False
        return True

    @staticmethod
    def _result(value: Any) -> dict[str, Any]:
        if isinstance(value, list):
            value = {"findings": value}
        if isinstance(value, dict) and any(key in value for key in ("findings", "observations")):
            findings = value.get("findings", value.get("observations", []))
            candidates = value.get("candidates", [])
            if (isinstance(findings, list) and isinstance(candidates, list)
                    and all(isinstance(row, dict) for row in findings + candidates)):
                result = {**value, "findings": findings[:32], "observations": findings[:32],
                          "candidates": candidates[:8]}
                result["status"] = ("skipped" if result.get("skipped") else "flagged" if findings
                                    else "review" if candidates else "clean")
                return result
        return {"skipped": True, "status": "skipped", "reason": "invalid_evaluator_result",
                "findings": [], "observations": [], "candidates": []}

    def _optional_llm(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        candidates = result.get("candidates") or []
        if not (self.llm_enable and candidates and self.llm_callback and self.loop and self.loop.is_running()):
            result["llm_used"] = False
            return
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(self.llm_callback(payload, list(candidates)), self.loop)
            rows = future.result(timeout=self.timeout)
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise ValueError("invalid LLM review result")
            result["findings"] = (result.get("findings", []) + rows)[:32]
            if result["findings"]:
                result["status"] = "flagged"
            result["llm_used"] = True
            result["llm_status"] = "completed"
        except TimeoutError:
            if future is not None:
                future.cancel()
            self.dropped["timeout"] += 1
            result["llm_used"] = True
            result["llm_status"] = "timeout"
            logger.warning("[consistency] optional review timed out; local result retained")
        except Exception as exc:
            self.dropped["errors"] += 1
            result["llm_used"] = True
            result["llm_status"] = "failed"
            result["llm_error_type"] = type(exc).__name__
            logger.warning("[consistency] optional review failed: %s", type(exc).__name__)

    def _process_pair(self, snapshot: dict[str, Any], response: dict[str, Any]) -> None:
        request_id = str(snapshot.get("request_id") or "")
        answer = str(response.get("answer") or "")
        inspected_answer = answer[:12000]
        payload = {**snapshot, **response, "request_id": request_id,
                   "query": snapshot.get("query", snapshot.get("query_text", "")),
                   "answer": inspected_answer,
                   "references": snapshot.get("references", snapshot.get("evidence", []))}
        started = time.perf_counter()
        try:
            result = self._result(self.evaluator(payload) if self.mode != "off" else {
                "skipped": True, "reason": "off", "findings": [], "observations": [], "candidates": []})
        except Exception as exc:
            self.dropped["errors"] += 1
            logger.warning("[consistency] evaluator failed: %s", type(exc).__name__)
            result = {"skipped": True, "status": "skipped", "reason": "evaluator_error",
                      "findings": [], "observations": [], "candidates": []}
        elapsed = time.perf_counter() - started
        if elapsed > self.timeout:
            self.dropped["timeout"] += 1
            result = {"skipped": True, "reason": "local_timeout", "status": "skipped",
                      "findings": [], "observations": [], "candidates": [],
                      "elapsed_ms": round(elapsed * 1000, 3)}
        else:
            self._optional_llm(payload, result)
        findings = [item for item in (result.get("findings") or []) if isinstance(item, dict)]
        skipped = bool(result.get("skipped"))
        fields = {
            "scope_id": str(snapshot.get("scope_id") or "default"),
            "answer_hash": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "answer_chars": len(answer),
            "answer_preview": answer[:240],
            "response_status": str(
                response.get("response_status")
                or ("completed" if answer else "empty")
            ),
            "chunk_status": str(response.get("chunk_status") or "final"),
            "observation_status": "skipped" if skipped else "checked",
            "skip_reason": str(result.get("reason") or "") if skipped else "",
            "consistency_status": str(result.get("status") or ("skipped" if skipped else "clean")),
            "response_truncated": int(bool(len(answer) > len(inspected_answer))),
            "inspected_chars": len(inspected_answer),
            "terminal": 1,
        }
        with self._lock:
            self._terminal[request_id] = time.time()
        recorder = getattr(self.store, "thread_record_consistency_result", None)
        if recorder is not None:
            recorder(request_id, findings, **fields)
        else:
            self.store.thread_record_consistency_observations(
                request_id, findings, scope_id=fields["scope_id"])
            self.store.thread_record_request_observation(request_id, **fields)

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                kind, item = self._queue.get(timeout=.05)
            except queue.Empty:
                expired = []
                with self._lock:
                    expired = self._prune_locked(time.time())
                for request_id, pair, reason in expired:
                    self._record_unpaired(request_id, pair, reason)
                continue
            try:
                request_id = str(item.get("request_id") or "")
                if kind == "observation":
                    self.store.thread_record_request_observation(
                        request_id,
                        **{key: value for key, value in item.items() if key != "request_id"},
                    )
                    continue
                expired = []
                with self._lock:
                    expired = self._prune_locked(time.time())
                    terminal = request_id in self._terminal
                for expired_id, pair, reason in expired:
                    self._record_unpaired(expired_id, pair, reason)
                if terminal:
                    continue
                if kind == "invalid_snapshot":
                    with self._lock:
                        self._pending.pop(request_id, None)
                    self._record_unpaired(request_id, {"snapshot": item}, "invalid_snapshot")
                    continue
                if kind == "snapshot":
                    self.store.thread_record_request_observation(request_id, **{
                        key: value for key, value in item.items() if key != "request_id"
                    })
                with self._lock:
                    if request_id in self._terminal:
                        continue
                    pair = self._pending.setdefault(request_id, {"_ts": time.time()})
                    pair[kind] = item
                    complete = "snapshot" in pair and "response" in pair
                    if complete:
                        snapshot, response = pair["snapshot"], pair["response"]
                        self._pending.pop(request_id, None)
                    expired = self._prune_locked(time.time())
                for expired_id, expired_pair, reason in expired:
                    self._record_unpaired(expired_id, expired_pair, reason)
                if complete:
                    self._process_pair(snapshot, response)
            except Exception as exc:
                self.dropped["errors"] += 1
                logger.warning("[consistency] worker item failed; persistence not guaranteed: %s", type(exc).__name__)
            finally:
                self._queue.task_done()
        # Only the worker finalizes unmatched pairs, after all accepted work.
        # A timed-out close must not steal a pair that is still being assembled.
        with self._lock:
            leftovers = list(self._pending.items())
            self._pending.clear()
        for request_id, pair in leftovers:
            reason = "missing_response" if "response" not in pair else "missing_snapshot"
            self._record_unpaired(request_id, pair, reason)

    def wait(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(.01, float(timeout))
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float = 2.0) -> bool:
        # Stop accepting submissions, drain already accepted work, then join.
        # EpisodicStore closes SQLite only after this method returns.
        with self._lock:
            self._closed = True
        self._stop.set()
        self._worker.join(max(.05, float(timeout)))
        return not self._worker.is_alive()

    def feedback(self, request_id: str, label: str, *, note: str = "", operator: str = "webui") -> int:
        """Persist a reversible human label for an existing request observation."""
        if str(label or "").strip() not in {"true_error", "false_positive", "uncertain", "irrelevant"}:
            raise ValueError("invalid consistency feedback label")
        recorder = getattr(self.store, "thread_record_consistency_feedback", None)
        if recorder is None:
            raise RuntimeError("consistency feedback storage unavailable")
        return int(recorder(request_id, label, note=note, operator=operator))

    def feedback_history(self, request_id: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        reader = getattr(self.store, "thread_list_consistency_feedback", None)
        if reader is None:
            return []
        return list(reader(request_id, limit=limit))

    def export(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a per-export, non-linkable evaluation projection."""
        rows = self.store.thread_list_consistency_observations(**kwargs)
        # The salt is intentionally fresh for every call: samples can be
        # grouped inside one export, but cannot be joined across exports.
        salt = secrets.token_bytes(32)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            request_id = str(row.get("request_id") or "")
            groups.setdefault(request_id, []).append(row)
        labels: dict[str, str] = {}
        reader = getattr(self.store, "thread_list_consistency_feedback", None)
        if reader is not None:
            for request_id in groups:
                history = reader(request_id, limit=1) or []
                if history:
                    labels[request_id] = str(history[0].get("label") or "")
        output = []
        for request_id, group in groups.items():
            sample = hmac.new(salt, request_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
            total = len(group)
            for index, row in enumerate(group, 1):
                raw_confidence = row.get("confidence")
                try:
                    confidence = 0.0 if raw_confidence is None else round(float(raw_confidence), 4)
                except (TypeError, ValueError):
                    confidence = 0.0
                finding_key = hmac.new(
                    salt, f"{request_id}:{index}:{row.get('error_type', '')}".encode("utf-8"), hashlib.sha256
                ).hexdigest()[:16]
                output.append({
                    "sample": sample,
                    "finding_key": finding_key,
                    "finding_index": index,
                    "finding_count": total,
                    "error_type": str(row.get("error_type") or ""),
                    "severity": str(row.get("severity") or "low"),
                    "confidence": confidence,
                    "decision_source": str(row.get("decision_source") or "rule"),
                    "label": labels.get(request_id) or None,
                })
        return output

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"mode": self.mode, "capacity": self.capacity, "queued": self._queue.qsize(),
                    "pending": len(self._pending), "seen": len(self._seen),
                    "terminal": len(self._terminal), "closed": self._closed,
                    "dropped": dict(self.dropped)}
