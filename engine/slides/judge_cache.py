"""Память о проверке фото героев между роликами.

Фото знаменитостей повторяются от ролика к ролику (поиск по тому же имени отдаёт те же снимки). Нейросеть уже
сказала про файл «водяной знак / надпись / коллаж» или «чистый» — запоминаем по отпечатку содержимого (sha1) и
в следующем ролике этот снимок не отправляем на проверку (токены и время). Запоминается только то, что не зависит
от героя и ролика: «плохой вид» (да/нет + причина). Файл — data/cache/judge_photos.json, до MAX_ENTRIES записей
(старые вытесняются). Битый файл — как будто его нет.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from engine.core import config as cfg_mod

MAX_ENTRIES = 30000
_lock = threading.Lock()
_mem: dict[str, list] | None = None
_dirty = False


def _file() -> Path:
    override = os.environ.get("STORY_VIDEO_JUDGE_CACHE_DIR")  # тесты — во временную папку
    return (Path(override) if override else cfg_mod.DATA_DIR / "cache") / "judge_photos.json"


def fingerprint(path: Path) -> str:
    """sha1 содержимого файла (фото ~100–300 КБ — миллисекунды)."""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load() -> dict[str, list]:
    global _mem
    if _mem is None:
        try:
            raw = json.loads(_file().read_text(encoding="utf-8"))
            _mem = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 2} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _mem = {}
    return _mem


def get(key: str) -> str | None:
    """Причина отказа («watermark…»), «» — кадр чистый, None — такого файла ещё не проверяли."""
    with _lock:
        rec = _load().get(key)
    return None if rec is None else str(rec[0])


def put(key: str, bad_reason: str) -> None:
    global _dirty
    with _lock:
        mem = _load()
        mem.pop(key, None)  # свежая запись — в конец (вытесняются самые старые)
        mem[key] = [bad_reason, int(time.time())]
        _dirty = True


def flush() -> None:
    """Записать на диск (раз за ролик, а не на каждый кадр)."""
    global _dirty
    with _lock:
        if not _dirty or _mem is None:
            return
        while len(_mem) > MAX_ENTRIES:
            _mem.pop(next(iter(_mem)))
        path = _file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(_mem, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            _dirty = False
        except OSError:
            pass  # кеш — ускорение, не данные: не записался — в следующий раз проверим заново


def reset_for_tests() -> None:
    global _mem, _dirty
    with _lock:
        _mem, _dirty = None, False
