"""ffmpeg / ffprobe."""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path


class FFmpegNotFoundError(RuntimeError):
    pass


_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",  # libass / subtitles
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)

_PROBE_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe",
    "/usr/local/opt/ffmpeg-full/bin/ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
)


def find_ffmpeg() -> str:
    # предпочитаем ffmpeg-full (есть subtitles/libass)
    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FFmpegNotFoundError(
        "ffmpeg не найден. Установи: brew install ffmpeg-full"
    )


def find_ffprobe() -> str:
    for candidate in _PROBE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    path = shutil.which("ffprobe")
    if path:
        return path
    raise FFmpegNotFoundError(
        "ffprobe не найден. Установи: brew install ffmpeg-full"
    )


NICE_BIN = "/usr/bin/nice"
RENDER_NICE = "10"


def low_priority(cmd: list[str]) -> list[str]:
    """Тяжёлая команда (кодирование ролика) — с пониженным приоритетом: Mac остаётся отзывчивым,
    браузер и сама программа не тормозят. Скорость сборки почти та же: ffmpeg и так занимает все ядра,
    приоритет влияет, только когда им делится кто-то ещё."""
    return [NICE_BIN, "-n", RENDER_NICE, *cmd] if Path(NICE_BIN).is_file() else list(cmd)


@functools.lru_cache(maxsize=8)
def has_filter(name: str, ffmpeg: str | None = None) -> bool:
    bin_path = ffmpeg or find_ffmpeg()
    try:
        proc = subprocess.run(
            [bin_path, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        return False
    needle = f" {name} "
    return needle in f" {text} "
