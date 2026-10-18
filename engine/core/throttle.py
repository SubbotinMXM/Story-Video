"""Тяжёлые для процессора этапы (рендер, сборка видео) — по одному на всё приложение.

Когда параллельно идут два ролика, остальное (озвучка, подбор кадров, LLM)
идёт одновременно, а рендеры не делят процессор пополам, а встают в очередь.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from engine.core.logutil import log

_HEAVY = threading.BoundedSemaphore(1)


@contextmanager
def heavy_stage(label: str, cancel=None):
    if not _HEAVY.acquire(blocking=False):
        log(f"{label}: жду, пока закончится рендер другого ролика…")
        while not _HEAVY.acquire(timeout=0.5):
            if cancel is not None:
                cancel.check()
    try:
        yield
    finally:
        _HEAVY.release()
