from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any, Callable

from .xinchao_engine import new_state, normalize_state


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._states: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            raw: Any = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    raw = {}
            if isinstance(raw, dict) and isinstance(raw.get("states"), dict):
                source = raw["states"]
            elif isinstance(raw, dict) and "drives" in raw:
                source = {"global": raw}
            else:
                source = {}
            self._states = {str(key): normalize_state(value) for key, value in source.items()}
            self._loaded = True

    async def keys(self) -> list[str]:
        await self.load()
        async with self._lock:
            return sorted(self._states)

    async def read(self, key: str) -> dict[str, Any]:
        await self.load()
        async with self._lock:
            if key not in self._states:
                self._states[key] = new_state()
                self._write_locked()
            return copy.deepcopy(self._states[key])

    async def update(self, key: str, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        await self.load()
        async with self._lock:
            current = copy.deepcopy(self._states.get(key) or new_state())
            updated = normalize_state(mutator(current))
            self._states[key] = updated
            self._write_locked()
            return copy.deepcopy(updated)

    async def replace(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        return await self.update(key, lambda _: value)

    async def reset(self, key: str) -> dict[str, Any]:
        return await self.replace(key, new_state())

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        await self.load()
        async with self._lock:
            return copy.deepcopy(self._states)

    def _write_locked(self) -> None:
        payload = {"formatVersion": 1, "states": self._states}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
