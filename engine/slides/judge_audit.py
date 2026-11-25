"""Контроль качества «лёгкой» модели в проверке кадров пула.

Кадры пула (проверка по теме ролика, водяным знакам, надписям) проверяет дешёвая модель (в ~5 раз дешевле «умной»).
Чтобы качество не просело незаметно, каждый N-й лист проверяется обеими моделями: решения (кадр годен / нет)
сравниваются, итог копится в data/cache/judge_audit.json (последние WINDOW сравнений). Дешёвая модель опасна тем, что
пропустит лишнее — поэтому следим за «ложными принятиями» (она приняла, умная отвергла). Их стало больше
MAX_FALSE_ACCEPT или решения расходятся чаще MAX_DISAGREE — пул сам возвращается на «умную» модель.
Сравниваемые листы берутся с умной модели (на них качество как раньше).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from engine.core import config as cfg_mod
from engine.core.logutil import log

WINDOW = 240  # сколько последних сравнений кадров храним
MIN_COMPARED = 36  # пока сравнений меньше — доверяем дешёвой (измерено: 54/60 совпало, ложных принятий 3%)
MAX_FALSE_ACCEPT = 0.10
MAX_DISAGREE = 0.25
SAMPLE_EVERY = 8  # каждый 8-й лист проверяется ещё и умной моделью

_lock = threading.Lock()
_mem: list[list[int]] | None = None  # [[дешёвая приняла, умная приняла], ...]
_sheets = 0
_dirty = False


def _file() -> Path:
    override = os.environ.get("STORY_VIDEO_JUDGE_CACHE_DIR")
    return (Path(override) if override else cfg_mod.DATA_DIR / "cache") / "judge_audit.json"


def _load() -> list[list[int]]:
    global _mem
    if _mem is None:
        try:
            raw = json.loads(_file().read_text(encoding="utf-8"))
            _mem = [[int(a), int(b)] for a, b in raw.get("pairs", [])][-WINDOW:]
        except (OSError, ValueError, TypeError):
            _mem = []
    return _mem


def stats() -> dict:
    with _lock:
        pairs = list(_load())
    n = len(pairs)
    false_acc = sum(1 for a, b in pairs if a and not b)
    differ = sum(1 for a, b in pairs if a != b)
    return {"compared": n, "false_accept": false_acc, "disagree": differ,
            "false_accept_rate": false_acc / n if n else 0.0, "disagree_rate": differ / n if n else 0.0}


def use_light() -> bool:
    """Можно ли проверять кадры пула дешёвой моделью: пока сравнений мало — да, потом по итогам сравнений."""
    s = stats()
    if s["compared"] < MIN_COMPARED:
        return True
    return s["false_accept_rate"] <= MAX_FALSE_ACCEPT and s["disagree_rate"] <= MAX_DISAGREE


def should_sample() -> bool:
    """Этот лист проверить ещё и умной моделью."""
    global _sheets
    with _lock:
        _sheets += 1
        return _sheets % SAMPLE_EVERY == 1


def record(pairs: list[tuple[bool, bool]]) -> None:
    """Решения дешёвой и умной по одним и тем же кадрам."""
    global _dirty
    was = use_light()
    with _lock:
        mem = _load()
        mem.extend([[int(a), int(b)] for a, b in pairs])
        del mem[:-WINDOW]
        _dirty = True
    s = stats()
    log(f"Контроль проверки кадров: дешёвая и умная модель совпали в {s['compared'] - s['disagree']} из {s['compared']}, "
        f"ложных принятий {s['false_accept']} ({s['false_accept_rate']:.0%})")
    if was and not use_light():
        log("Контроль проверки кадров: дешёвая модель пропускает лишнее — кадры пула дальше проверяет умная модель")
    flush()


def flush() -> None:
    global _dirty
    with _lock:
        if not _dirty or _mem is None:
            return
        path = _file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps({"pairs": _mem}), encoding="utf-8")
            tmp.replace(path)
            _dirty = False
        except OSError:
            pass


def reset_for_tests() -> None:
    global _mem, _sheets, _dirty
    with _lock:
        _mem, _sheets, _dirty = None, 0, False
