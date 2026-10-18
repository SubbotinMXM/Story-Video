"""Фоновая музыка для стоковых роликов: каталог assets/music (FreePD, общественное достояние CC0).

Треки заранее подготовлены tools/prepare_music.py: одна громкость (−16 LUFS), ровная
по всей длине, и бесшовный стык для повтора по кругу.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from engine.core.config import PROJECT_ROOT

MUSIC_DIR = PROJECT_ROOT / "assets" / "music"
# громкость музыки под голосом: ~−20 дБ — чистый фон, чтец всегда слышен
MUSIC_VOLUME = 0.1


@lru_cache(maxsize=1)
def catalog() -> dict:
    path = MUSIC_DIR / "catalog.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"groups": {}, "tracks": []}
    tracks = [t for t in raw.get("tracks") or [] if (MUSIC_DIR / str(t.get("file") or "")).is_file()]
    return {"groups": raw.get("groups") or {}, "tracks": tracks}


def track(track_id: str) -> dict | None:
    tid = (track_id or "").strip()
    return next((t for t in catalog()["tracks"] if t["id"] == tid), None) if tid else None


def track_path(track_id: str) -> Path | None:
    t = track(track_id)
    return MUSIC_DIR / t["file"] if t else None


def credit_line(track_id: str) -> str:
    t = track(track_id)
    if not t:
        return ""
    return f"Музыка: «{t['title']}» — FreePD.com, общественное достояние (CC0), указывать автора не обязательно."
