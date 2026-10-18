"""Рассылка событий в открытые SSE-соединения (из любых потоков)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from typing import Any

LOG_BACKLOG = 1000


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []
        self.logs: deque[dict[str, Any]] = deque(maxlen=LOG_BACKLOG)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subs = [(lp, q) for lp, q in self._subs if q is not queue]

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "log":
            with self._lock:
                self.logs.append(data)
        payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
        with self._lock:
            subs = list(self._subs)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(_put_nowait, queue, payload)
            except RuntimeError:
                # loop уже закрыт — соединение отвалилось
                self.unsubscribe(queue)


def _put_nowait(queue: asyncio.Queue[str], payload: str) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        pass


hub = EventHub()
