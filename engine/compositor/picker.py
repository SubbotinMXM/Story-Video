"""Выбор клипов и случайных эффектов."""

from __future__ import annotations

import random
from pathlib import Path

from engine.compositor.defaults import BROLL_COUNT
from engine.core.spread import spread
from engine.compositor.utils import list_videos, list_videos_soft

# Лёгкие фильтры только на b-roll фон
def pick_head(folder: Path | None, rng: random.Random | None = None) -> Path | None:
    """Случайное видео головы диктора из папки.

    Если папка не задана / не является директорией или в ней нет видео — None.
    """
    if folder is None or not folder.is_dir():
        return None
    rng = rng or random.Random()
    files = list_videos(folder)
    if not files:
        return None
    return rng.choice(files)


def pick_folder_video(
    folder: Path | None, rng: random.Random | None = None
) -> Path | None:
    """Случайное видео из папки. None если путь пуст / нет файлов."""
    if folder is None or not folder.is_dir():
        return None
    files = list_videos_soft(folder)
    if not files:
        return None
    rng = rng or random.Random()
    return rng.choice(files)


def pick_outro(folder: Path | None, rng: random.Random | None = None) -> Path | None:
    """Случайное видео из папки аутро. None если путь пуст / нет файлов."""
    return pick_folder_video(folder, rng)


def pick_intro(folder: Path | None, rng: random.Random | None = None) -> Path | None:
    """Случайное видео из папки интро. None если путь пуст / нет файлов."""
    return pick_folder_video(folder, rng)


def pick_broll(folder: Path, rng: random.Random | None = None) -> list[Path]:
    rng = rng or random.Random()
    files = list_videos(folder)
    count = BROLL_COUNT
    if len(files) >= count:
        return rng.sample(files, count)
    # меньше файлов — каждый файл поровну (разница не больше одного раза),
    # одинаковые не подряд
    picked: list[Path] = []
    while len(picked) < count:
        batch = list(files)
        rng.shuffle(batch)
        picked.extend(batch)
    return spread(picked[:count], rng=rng)
