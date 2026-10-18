"""Пути run-папок и Desktop."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from engine.core.config import DATA_DIR, PROJECT_ROOT

RUNS_DIR = DATA_DIR / "runs"
DESKTOP = Path.home() / "Desktop"

DEFAULT_SUBSCRIBE_PATH = PROJECT_ROOT / "assets" / "subscribe.mp4"

SCRIPT_NAME = "script.txt"
AUDIO_NAME = "voice.mp3"
TIMINGS_NAME = "timings.json"
TIMINGS_PREVIEW_NAME = "timings_preview.txt"
CHUNKS_NAME = "chunks.json"
CHUNKS_PREVIEW_NAME = "chunks_preview.txt"
TIMELINE_NAME = "timeline.json"
META_NAME = "meta.json"
CHECKPOINT_NAME = "checkpoint.json"

_SAFE = re.compile(r"[^\w\-]+", re.UNICODE)


def ensure_runs_dir() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR


def new_run_dir(*, title_hint: str = "") -> Path:
    ensure_runs_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slug(title_hint, max_len=40)
    name = f"{stamp}_{slug}" if slug else stamp
    return mkdir_unique(RUNS_DIR, name, sep="_")


def mkdir_unique(parent: Path, name: str, *, sep: str) -> Path:
    """Создать новую папку; занято (два ролика в одну секунду) — name_2, name_3…"""
    parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = parent / (name if n == 1 else f"{name}{sep}{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1


def desktop_copy_name(stem: str, suffix: str) -> Path:
    """Уникальное имя на рабочем столе."""
    DESKTOP.mkdir(parents=True, exist_ok=True)
    safe = _slug(stem, max_len=48) or "video"
    candidate = DESKTOP / f"{safe}{suffix}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = DESKTOP / f"{safe}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _slug(text: str, *, max_len: int) -> str:
    t = (text or "").strip().lower().replace(" ", "-")
    t = _SAFE.sub("-", t).strip("-")
    return t[:max_len].rstrip("-")


def latest_run_with_file(name: str) -> Path | None:
    """Самый свежий run, где есть указанный файл."""
    if not RUNS_DIR.is_dir():
        return None
    runs = sorted(
        (p for p in RUNS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for run in runs:
        if (run / name).is_file():
            return run
    return None
