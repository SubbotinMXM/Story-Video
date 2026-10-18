"""SRT из timings.json. Если есть авторский текст — им заменяем слова Whisper."""

from __future__ import annotations

import json
import re
from pathlib import Path

_WORD = re.compile(r"\S+")
ORIGIN_NAME = "script_origin.txt"


def mark_script_origin(run_dir: Path, origin: str) -> None:
    (run_dir / ORIGIN_NAME).write_text(origin.strip() + "\n", encoding="utf-8")


def timings_to_srt(
    timings_path: Path,
    dest: Path,
    *,
    max_words: int = 8,
    script_path: Path | None = None,
    run_dir: Path | None = None,
) -> Path | None:
    """Группирует слова в фразы ~max_words → SRT. None если пусто."""
    if not timings_path.is_file():
        return None
    raw = json.loads(timings_path.read_text(encoding="utf-8"))
    words = raw.get("words") if isinstance(raw, dict) else None
    if not isinstance(words, list) or not words:
        return None
    origin = ""
    if run_dir is not None:
        marker = run_dir / ORIGIN_NAME
        if marker.is_file():
            origin = marker.read_text(encoding="utf-8").strip()
    if origin == "user" and script_path is not None and script_path.is_file():
        script = script_path.read_text(encoding="utf-8")
        words = align_script_words(script, words)

    phrases: list[tuple[float, float, str]] = []
    buf: list[str] = []
    start = 0.0
    end = 0.0
    for i, item in enumerate(words):
        if not isinstance(item, dict):
            continue
        w = str(item.get("word") or "").strip()
        if not w:
            continue
        s = float(item.get("start") or 0.0)
        e = float(item.get("end") or s)
        if not buf:
            start = s
        buf.append(w)
        end = e
        punct = w[-1] in ".!?…。" if w else False
        if len(buf) >= max_words or punct:
            phrases.append((start, end, " ".join(buf)))
            buf = []
    if buf:
        phrases.append((start, end, " ".join(buf)))

    if not phrases:
        return None

    lines: list[str] = []
    for i, (s, e, text) in enumerate(phrases, 1):
        lines.append(str(i))
        lines.append(f"{_ts(s)} --> {_ts(e)}")
        lines.append(text)
        lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def align_script_words(script: str, whisper_words: list) -> list[dict]:
    """Тайминги Whisper, текст — из сценария. Орфография берётся не из распознавания."""
    tokens = _WORD.findall(script or "")
    timed = [
        item
        for item in whisper_words
        if isinstance(item, dict) and str(item.get("word") or "").strip()
    ]
    if not tokens or not timed:
        return [item for item in whisper_words if isinstance(item, dict)]
    n_s = len(tokens)
    n_w = len(timed)
    out: list[dict] = []
    for i, token in enumerate(tokens):
        j0 = min(n_w - 1, int(i * n_w / n_s))
        j1 = min(n_w, max(j0 + 1, int((i + 1) * n_w / n_s)))
        start = float(timed[j0].get("start") or 0.0)
        end = float(timed[j1 - 1].get("end") or start)
        if end <= start:
            end = start + 0.18
        out.append({"word": token, "start": start, "end": end})
    return out


def _ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
