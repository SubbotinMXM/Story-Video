"""Логи в файл + опциональный callback в UI."""

from __future__ import annotations

import contextvars
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

LogCb = Callable[[str], None]

_lock = threading.Lock()
_callback: LogCb | None = None

# STORY_VIDEO_LOG — тесты пишут в свой файл, а не в журнал приложения
LOG_PATH = Path(os.environ.get("STORY_VIDEO_LOG") or Path(__file__).resolve().parents[2] / "data" / "app.log")


# id задачи, к которой относится текущий код (переносится в рабочие потоки,
# см. engine.core.threads) — журнал каждой задачи отдельно, даже если идут две
current_job: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_job", default=None)


def set_log_callback(cb: LogCb | None) -> None:
    global _callback
    _callback = cb


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        cb = _callback
    print(line, flush=True)
    if cb:
        try:
            cb(line)
        except Exception:
            pass
