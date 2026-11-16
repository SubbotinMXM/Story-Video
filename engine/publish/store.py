"""Где выкладка хранит свой учёт: data/publish (тесты — во временной папке)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from engine.core.config import DATA_DIR

# один замок на весь учёт выкладки: выбор обложки, номера, даты — без гонок между задачами
LOCK = threading.RLock()


def publish_dir() -> Path:
    return Path(os.environ.get("STORY_VIDEO_PUBLISH_DIR") or DATA_DIR / "publish")


def read_json(name: str, default):
    path = publish_dir() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def write_json(name: str, data) -> None:
    path = publish_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
