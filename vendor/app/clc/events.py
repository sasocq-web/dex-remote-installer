from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Set


LOGGER = logging.getLogger(__name__)
CriticalObserver = Callable[[Dict[str, Any]], None]


class EventHub:
    """Fan out events without letting slow consumers block Codex.

    Queue subscribers are deliberately lossy. Critical lifecycle state must
    therefore use a synchronous observer, which runs before an event can be
    discarded from any subscriber queue.
    """

    def __init__(self, queue_size: int = 1000) -> None:
        self._clients: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._critical_observers: Set[CriticalObserver] = set()
        self._lock = asyncio.Lock()
        self._queue_size = queue_size

    def add_critical_observer(self, observer: CriticalObserver) -> None:
        self._critical_observers.add(observer)

    def remove_critical_observer(self, observer: CriticalObserver) -> None:
        self._critical_observers.discard(observer)

    async def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._clients.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        async with self._lock:
            self._clients.discard(queue)

    async def publish(self, event: Dict[str, Any]) -> None:
        for observer in tuple(self._critical_observers):
            try:
                observer(event)
            except Exception:
                # Auxiliary delivery must continue even if a critical observer
                # fails. The exception remains visible in the service log.
                LOGGER.exception("Falha no observador crítico de eventos")
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
