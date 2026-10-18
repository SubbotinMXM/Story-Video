"""Расширенные ffmpeg-утилиты."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engine.slides.ffmpeg_util import find_ffmpeg, find_ffprobe

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def probe_duration(path: Path) -> float:
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"Нулевая длительность: {path}")
    return duration


def has_audio_stream(path: Path) -> bool:
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def probe_video_size(path: Path) -> tuple[int, int] | None:
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        streams = (json.loads(result.stdout).get("streams") or [{}])
        st = streams[0] if streams else {}
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h


def is_landscape(path: Path, *, min_ratio: float = 1.25) -> bool:
    """Горизонталь: width/height >= min_ratio. Вертикаль и почти-квадрат — нет."""
    size = probe_video_size(path)
    if not size:
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            return False
    else:
        w, h = size
    if w <= 0 or h <= 0:
        return False
    return (w / h) >= min_ratio


def is_landscape_16x9(path: Path, *, tol: float = 0.12) -> bool:
    """Только горизонтальные ~16:9. Вертикаль и квадрат — нет."""
    size = probe_video_size(path)
    if not size:
        return False
    w, h = size
    ratio = w / h
    return abs(ratio - (16 / 9)) <= tol and w >= h


DEFAULT_MIN_VIDEO_HEIGHT = 720


def video_height_ok(path: Path, min_height: int) -> bool:
    """Видео не ниже min_height по высоте (720 → минимум 1280x720)."""
    if min_height <= 0:
        return True
    size = probe_video_size(path)
    if not size:
        return False
    return size[1] >= min_height


def candidate_height_ok(width: int, height: int, min_height: int) -> bool:
    """Префильтр до скачивания: размер неизвестен — пропускаем, проверим файл."""
    if min_height <= 0 or width <= 0 or height <= 0:
        return True
    return height >= min_height


def candidate_is_landscape(width: int, height: int, *, min_ratio: float = 1.25) -> bool:
    """True если размер неизвестен или уже горизонталь (для префильтра поиска)."""
    if width <= 0 or height <= 0:
        return True
    return (width / height) >= min_ratio


LOW_DISK_BYTES = 2 * 1024**3


def disk_full_hint(cmd: list[str], err: str = "") -> str:
    """Понятная причина, если ffmpeg упал из-за нехватки места на диске."""
    import shutil

    out = Path(cmd[-1]) if cmd else None
    folder = out.parent if out is not None and out.parent.exists() else Path.home()
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return ""
    if "No space left" not in err and free >= LOW_DISK_BYTES:
        return ""
    return (
        f"Не хватило места на диске: свободно {free / 1024**3:.1f} ГБ. "
        "Освободи место (длинный ролик со сборкой занимает 5–10 ГБ) и нажми «Продолжить с места падения» — "
        "готовые этапы не переделываются"
    )


def run_ffmpeg(cmd: list[str], *, label: str = "ffmpeg") -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-1200:]
        hint = disk_full_hint(cmd, err)
        if hint:
            raise RuntimeError(hint)
        raise RuntimeError(f"{label} failed:\n{err}")


def concat_demuxer(parts: list[Path], dest: Path, *, reencode: bool = False) -> None:
    """Склеить сегменты одинакового формата."""
    if not parts:
        raise ValueError("Нет сегментов для склейки")
    if len(parts) == 1:
        dest.write_bytes(parts[0].read_bytes())
        return

    ffmpeg = find_ffmpeg()
    list_file = dest.parent / f"{dest.stem}_concat.txt"
    lines = []
    for p in parts:
        escaped = str(p.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if reencode:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-an",
            str(dest),
        ]
    else:
        cmd += ["-c", "copy", str(dest)]
    try:
        run_ffmpeg(cmd, label="concat")
    except RuntimeError:
        # fallback re-encode
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-an",
            str(dest),
        ]
        run_ffmpeg(cmd, label="concat-reencode")
    finally:
        list_file.unlink(missing_ok=True)
