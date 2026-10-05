"""Track synchronous work whose thread survives cancellation of its caller."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class BackgroundWork:
    def __init__(self, capacity: int = 4) -> None:
        self.capacity = max(1, int(capacity))
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self.rejected = 0

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.closed or len(self.tasks) >= self.capacity:
            self.rejected += 1
            raise RuntimeError("background work closed or at capacity")
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    def _finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def drain(self, timeout: float = 30.0) -> bool:
        self.closed = True
        if self.tasks:
            await asyncio.wait(tuple(self.tasks), timeout=max(0.0, timeout))
        return not self.tasks

    def after_drain(self, callback: Callable[[], Any]) -> None:
        pending = set(self.tasks)
        if not pending:
            callback()
            return

        def finished(task: asyncio.Task) -> None:
            pending.discard(task)
            if not pending:
                callback()

        for task in pending:
            task.add_done_callback(finished)

    def status(self) -> dict[str, Any]:
        return {"active": len(self.tasks), "capacity": self.capacity,
                "closed": self.closed, "rejected": self.rejected}
