"""«Как выглядит» кадр: один отпечаток на фото и видео — чтобы одинаковое не попадало дважды.

Разные файлы бывают одной и той же картинкой: одно фото с двух сайтов, один клип
со стока, взятый и в начале, и в блоке, соседние дубли одной съёмки на Pexels.
Сравнение по пути их не ловит — сравниваем по виду (dHash 8×8, устойчив к размеру
и пережатию). Для видео — кадр из середины.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from engine.slides.ffprobe_util import grab_first_frame

# из 64 бит: ближе — та же картинка (пересжата, другой размер, рамка)
DUP_BITS = 10
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
_cache: dict[tuple[str, float], int | None] = {}
_lock = threading.Lock()


def image_hash(path: Path) -> int | None:
    """dHash 8×8 картинки."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            g = img.convert("L").resize((9, 8))
            px = list(g.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def _video_hash(path: Path) -> int | None:
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "f.jpg"
        # середина короткого клипа; совсем короткий — начало
        return image_hash(frame) if grab_first_frame(path, frame, times=(2.0, 0.5), quality=4) else None


def fingerprint(path: Path) -> int | None:
    """Отпечаток фото или видео (с кешем по пути и времени изменения)."""
    try:
        key = (str(path.resolve()), path.stat().st_mtime)
    except OSError:
        return None
    with _lock:
        if key in _cache:
            return _cache[key]
    fp = _video_hash(path) if path.suffix.lower() in _VIDEO_EXT else image_hash(path)
    with _lock:
        _cache[key] = fp
    return fp


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def similar(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and hamming(a, b) <= DUP_BITS


def seen(fp: int | None, looks: list[int | None]) -> bool:
    """Такой кадр (или почти такой) уже есть."""
    return fp is not None and any(similar(fp, x) for x in looks)


def look_groups(paths: list[Path]) -> dict[Path, int]:
    """Номер группы «одинаковых на вид» для каждого файла: для расстановки без повторов подряд."""
    fps = {p: fingerprint(p) for p in dict.fromkeys(paths)}
    groups: dict[Path, int] = {}
    reps: list[tuple[int, int | None]] = []
    for p, fp in fps.items():
        for gid, rep in reps:
            if similar(fp, rep):
                groups[p] = gid
                break
        else:
            gid = len(reps)
            reps.append((gid, fp))
            groups[p] = gid
    return groups
