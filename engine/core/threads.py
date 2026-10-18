"""Пул потоков, который переносит контекст (какая задача) в рабочие потоки.

Обычный ThreadPoolExecutor теряет contextvars: строки журнала из потоков
подбора кадров не знали бы, к какому ролику относятся, когда роликов несколько.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor as _Base


class ThreadPoolExecutor(_Base):
    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
