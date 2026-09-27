"""Keep slow, read-only extension discovery alive beyond the UI response budget."""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

@dataclass
class Entry:
    task: asyncio.Task
    created: float
    delivered: bool = False

class DeferredCatalogQueries:
    def __init__(self, ttl: float = 30):
        self.entries: dict[tuple, Entry] = {}
        self.ttl = ttl

    async def query(self, target: Any, method: str, params: dict, *,
                    refresh: bool = False, wait_timeout: float = 5,
                    rpc_timeout: float = 90) -> dict:
        normalized = {k: v for k, v in params.items() if k not in {"forceRefetch", "forceRefresh"}}
        key = (id(target), target.generation, method, json.dumps(normalized, sort_keys=True))
        now = time.monotonic()
        for old_key, old in list(self.entries.items()):
            if old_key != key and old.task.done() and now - old.created > 180:
                self.entries.pop(old_key, None)
        entry = self.entries.get(key)
        if entry and entry.task.done() and entry.delivered:
            ttl = self.ttl if entry.task.result().get("ok") else 5
            if refresh or now - entry.created > ttl:
                entry = None
        if entry is None:
            async def run():
                try:
                    result = await target.request(method, params, timeout=rpc_timeout)
                    return {"ok": True, "result": result, "pending": False}
                except Exception as exc:
                    return {"ok": False, "result": {}, "error": str(exc).strip() or type(exc).__name__, "pending": False}
            entry = Entry(asyncio.create_task(run(), name="extension-discovery"), now)
            self.entries[key] = entry
        try:
            result = await asyncio.wait_for(asyncio.shield(entry.task), timeout=wait_timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "result": {}, "error": "", "pending": True}
        entry.delivered = True
        return result
