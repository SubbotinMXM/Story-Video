"""Утилиты: ffprobe, пути."""

from __future__ import annotations

import subprocess
from pathlib import Path

from engine.compositor.defaults import AUDIO_EXTS, VIDEO_EXTS
from engine.slides.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from engine.slides.ffprobe_util import has_audio_stream, probe_duration


def list_videos(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {folder}")
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not files:
        raise FileNotFoundError(f"В папке нет видео: {folder}")
    return files


def list_videos_soft(folder: Path) -> list[Path]:
    """Как list_videos, но без исключений: нет папки/файлов → []."""
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


def probe_chromakey_crop(
    path: Path,
    *,
    chroma: str,
    similarity: float,
    blend: float,
    pad: int = 16,
    alpha_threshold: int = 30,
) -> tuple[int, int, int, int] | None:
    """Crop (x, y, w, h) по непрозрачным пикселям после chromakey.

    Сэмплируем несколько кадров — курсор/кнопка гуляют по кадру.
    Без контента → None (тогда overlay идёт без crop).
    """
    from PIL import Image

    ffmpeg = find_ffmpeg()
    duration = probe_duration(path)
    if duration <= 0:
        return None

    # Середина клипа + края «живой» части (у AAA края пустые).
    stamps = sorted(
        {
            max(0.05, duration * f)
            for f in (0.15, 0.3, 0.45, 0.55, 0.7, 0.85)
        }
    )

    minx = miny = 10**9
    maxx = maxy = -1
    width = height = 0

    for t in stamps:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(path),
            "-vf",
            f"chromakey={chroma}:{similarity}:{blend},format=rgba",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode != 0 or not result.stdout:
            continue
        from io import BytesIO

        im = Image.open(BytesIO(result.stdout)).convert("RGBA")
        width, height = im.size
        px = im.load()
        assert px is not None
        for y in range(height):
            for x in range(width):
                if px[x, y][3] > alpha_threshold:
                    if x < minx:
                        minx = x
                    if y < miny:
                        miny = y
                    if x > maxx:
                        maxx = x
                    if y > maxy:
                        maxy = y

    if maxx < minx or width <= 0:
        return None

    x0 = max(0, minx - pad)
    y0 = max(0, miny - pad)
    x1 = min(width - 1, maxx + pad)
    y1 = min(height - 1, maxy + pad)
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1
