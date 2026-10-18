"""Папка результата на Desktop для режима футажей."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from engine.slides.paths import mkdir_unique


def desktop_dir() -> Path:
    return Path.home() / "Desktop"


def sanitize_folder_name(text: str, *, max_len: int = 80) -> str:
    s = " ".join((text or "").split())
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "-")
    s = s.strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "ролик"


def run_output_dir(title_hint: str, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    name = f"{sanitize_folder_name(title_hint)}_{when.strftime('%Y%m%d-%H%M%S')}"
    return mkdir_unique(desktop_dir(), name, sep="_")


def finalize_run_dir(
    out_dir: Path, title_hint: str, when: datetime | None = None
) -> Path:
    when = when or datetime.now()
    target = (
        desktop_dir()
        / f"{sanitize_folder_name(title_hint)}_{when.strftime('%Y%m%d-%H%M%S')}"
    )
    if out_dir.resolve() == target.resolve():
        return out_dir
    if target.exists():
        n = 2
        while True:
            alt = target.with_name(f"{target.name}_{n}")
            if not alt.exists():
                target = alt
                break
            n += 1
    out_dir.rename(target)
    return target


def story_output_path(*, out_dir: Path, title_hint: str) -> Path:
    return out_dir / f"{sanitize_folder_name(title_hint)}.txt"


def video_output_path(*, out_dir: Path, title_hint: str) -> Path:
    return out_dir / f"{sanitize_folder_name(title_hint)}.mp4"


def audio_output_path(*, out_dir: Path, title_hint: str) -> Path:
    return out_dir / f"{sanitize_folder_name(title_hint)}.mp3"
