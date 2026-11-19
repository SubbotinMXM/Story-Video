"""Кэш ответов поиска YouTube (search.list): одинаковый запрос в течение TTL повторно квоту не тратит.

Поиск стоит 100 единиц (у Google для него ещё и отдельный суточный лимит), а повторяются запросы часто: задачу
перезапустили, поиск ниш запустили заново с теми же ключевыми словами, в слайдах два сегмента просят одно и то же.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

TTL = 12 * 3600
MAX_FILES = 300


def _dir() -> Path:
    from engine.core import config as cfg_mod

    return Path(os.environ.get("STORY_VIDEO_YT_CACHE_DIR") or (cfg_mod.DATA_DIR / "cache" / "yt_search"))


def _path(params: dict) -> Path:
    clean = {k: v for k, v in params.items() if k != "key"}  # ключ ответ не меняет
    return _dir() / (hashlib.sha1(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest() + ".json")


def _off() -> bool:
    return os.environ.get("STORY_VIDEO_YT_CACHE_OFF") == "1"  # тесты: ответы одного теста не должны попадать в другой


def get(params: dict, now: float | None = None) -> dict | None:
    if _off():
        return None
    p = _path(params)
    try:
        if (time.time() if now is None else now) - p.stat().st_mtime > TTL:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def put(params: dict, data: dict) -> None:
    if _off():
        return
    p = _path(params)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        files = sorted(p.parent.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for old in files[:-MAX_FILES]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # кэш — только экономия: не получилось записать, работаем без него
