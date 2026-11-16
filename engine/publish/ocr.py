"""Буквальное чтение надписей на картинке — встроенным распознаванием текста macOS (Vision).

Нейросеть с «глазами» читает по смыслу и сама исправляет испорченные буквы («СЛУЧАЙЊЫЕ» видит как
«СЛУЧАЙНЫЕ»). Vision с выключенной автокоррекцией читает как нарисовано — им и ловим ошибки в надписях.
Помощник на Swift собирается один раз (swiftc из Command Line Tools) в data/bin. Нет macOS/swiftc —
read() вернёт None, и проверка остаётся только на нейросети.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from engine.core.logutil import log

SOURCE = Path(__file__).resolve().parents[2] / "tools" / "mac_ocr.swift"
LANGS = {"ru": "ru-RU", "uk": "uk-UA", "en": "en-US", "es": "es-ES", "pt": "pt-BR", "de": "de-DE",
         "fr": "fr-FR", "it": "it-IT", "zh": "zh-Hans", "ja": "ja-JP", "ko": "ko-KR", "vi": "vi-VN"}
_broken = False


def _binary() -> Path | None:
    global _broken
    if _broken or os.environ.get("STORY_VIDEO_OCR_OFF") or platform.system() != "Darwin" or not SOURCE.is_file():
        return None
    from engine.core.config import DATA_DIR

    out = Path(os.environ.get("STORY_VIDEO_OCR_BIN") or DATA_DIR / "bin" / "mac_ocr")
    if out.is_file() and out.stat().st_mtime >= SOURCE.stat().st_mtime:
        return out
    swiftc = shutil.which("swiftc")
    if not swiftc:
        _broken = True
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([swiftc, "-O", str(SOURCE), "-o", str(out)], check=True, capture_output=True, timeout=300)
    except Exception as exc:
        log(f"Обложка: распознавание текста macOS не собралось ({str(exc)[:120]}) — проверка надписей только нейросетью")
        _broken = True
        return None
    return out


class Line(NamedTuple):
    text: str
    box: tuple[float, float, float, float]  # x, y, ширина, высота — доли картинки, y сверху вниз


def read(image: Path, language: str) -> list[Line] | None:
    """Строки надписей как нарисованы, с рамками. None — распознавание недоступно."""
    exe = _binary()
    if exe is None:
        return None
    code = (language or "").split("-")[0].lower()
    langs = ",".join(dict.fromkeys([LANGS.get(code, "en-US"), "en-US"]))
    try:
        res = subprocess.run([str(exe), str(image), langs], capture_output=True, text=True, timeout=60)
    except Exception as exc:
        log(f"Обложка: распознавание текста не вышло ({str(exc)[:100]})")
        return None
    if res.returncode != 0:
        return None
    out: list[Line] = []
    for raw in res.stdout.splitlines():
        text, _, box = raw.partition("\t")
        if not text.strip():
            continue
        try:
            x, y, w, h = (float(v) for v in box.split(","))
        except ValueError:
            x, y, w, h = 0.0, 0.0, 1.0, 1.0
        out.append(Line(text.strip(), (x, 1.0 - y - h, w, h)))
    return out


def crop(image: Path, box: tuple[float, float, float, float], *, pad: float = 0.35, width: int = 1600) -> bytes:
    """Крупный фрагмент картинки вокруг строки (с полями) — JPEG, чтобы буквы было видно под лупой."""
    import io

    from PIL import Image

    with Image.open(image) as im:
        im = im.convert("RGB")
        W, H = im.size
        x, y, w, h = box
        px, py = w * W * pad / 2 + 8, h * H * pad + 8
        left, top = max(0, int(x * W - px)), max(0, int(y * H - py))
        right, bottom = min(W, int((x + w) * W + px)), min(H, int((y + h) * H + py))
        part = im.crop((left, top, right, bottom))
        if part.width < width:
            k = width / part.width
            part = part.resize((width, max(1, int(part.height * k))), Image.LANCZOS)
        buf = io.BytesIO()
        part.save(buf, "JPEG", quality=92)
        return buf.getvalue()
