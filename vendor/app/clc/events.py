from __future__ import annotations

import asyncio
from typing import Any, Dict, Set


class EventHub:
    """Small in-memory fan-out hub for browser WebSocket clients."""

    def __init__(self, queue_size: int = 1000) -> None:
        self._clients: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._queue_size = queue_size

    async def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._clients.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        async with self._lock:
            self._clients.discard(queue)

    async def publish(self, event: Dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        for queue in clients:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow/disconnected client must never block the Codex event reader.
                pass
