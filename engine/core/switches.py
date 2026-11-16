"""Автосмена инструмента или модели (Claude → Codex → встроенная нейросеть, модель недоступна → другая):
программа сообщает об этом пользователю — что сменилось, на что, почему и в какой задаче.

Движок вызывает report(); очередь задач (server.jobs) подписывается через set_callback и показывает это
в карточке задачи и всплывающим уведомлением. Вне задачи (проверки, тесты) — только журнал.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from engine.core.logutil import current_job, log

# порядок инструментов по подписке и запасных: сначала Claude, потом Codex (личный ChatGPT), потом встроенная
SUB_ORDER = ("claude_code", "codex")
NAMES = {"claude_code": "Claude", "codex": "Codex (ChatGPT)", "llm": "встроенная нейросеть"}

Cb = Callable[[str | None, dict], None]
_cb: Cb | None = None
_lock = threading.Lock()


def set_callback(cb: Cb | None) -> None:
    global _cb
    _cb = cb


def name(tool: str) -> str:
    return NAMES.get(tool, tool)


def report(task: str, frm: str, to: str, reason: str) -> None:
    """task — что делается («рерайт», «разбор исходника», «проверка рерайта»…); frm/to — инструмент или модель
    (ключи claude_code/codex/llm заменяются понятными именами); reason — почему сменили."""
    item = {"task": task, "from": name(frm), "to": name(to), "reason": reason.strip()[:240], "at": time.time()}
    log(f"⇄ {task}: {item['from']} → {item['to']} — {item['reason']}")
    with _lock:
        cb = _cb
    if cb is not None:
        try:
            cb(current_job.get(), item)
        except Exception:  # noqa: BLE001 — уведомление не должно ронять работу
            pass


def order_from(first: str) -> list[str]:
    """Подписки по приоритету, начиная с выбранной: claude_code → [claude_code, codex]; codex → [codex, claude_code]."""
    rest = [e for e in SUB_ORDER if e != first]
    return ([first] if first in SUB_ORDER else []) + rest
