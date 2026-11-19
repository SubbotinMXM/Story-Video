"""«Крючок»: первые минуты ролика — самые красивые кадры.

Первые HOOK_SEC решают, останется ли зритель. В режимах без привязки к тексту
(«По смыслу ролика», блок после начала) в начало ставим самые красивые кадры пула
(CLIP: пролёты, виды, золотой час — выше; мыло, текст, скучное — ниже).
В строгих режимах под первые куски перебираются кандидаты и берётся красивейший
(см. strict_media).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from engine.core.logutil import log
from engine.slides.ffprobe_util import grab_first_frame

HOOK_SEC = 150.0
# первые 2 минуты — только живые кадры (AI-кадры — позже), видео в приоритете, фото — если видео нет
NO_AI_SEC = 120.0
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}


def add_beauty(items: list[dict], base_dir: Path) -> None:
    """Проставить item["beauty"] (CLIP) тем, у кого ещё нет. Видео — по кадру из середины."""
    from engine.slides.clip_ranker import beauty_scores

    todo = [it for it in items if "beauty" not in it and (base_dir / str(it.get("path") or "")).is_file()]
    if not todo:
        return
    with tempfile.TemporaryDirectory() as tmp:
        frames: dict[int, Path] = {}
        for k, it in enumerate(todo):
            path = base_dir / str(it["path"])
            if path.suffix.lower() not in _VIDEO_EXT:
                frames[k] = path
                continue
            frame = Path(tmp) / f"{k}.jpg"
            if grab_first_frame(path, frame):
                frames[k] = frame
        scores = beauty_scores(list(frames.values()))
        for k, it in enumerate(todo):
            it["beauty"] = round(scores.get(frames.get(k, Path("-")), 0.0), 4)
    log(f"Красота кадров: оценено {len(todo)}")


def head_rank(item: dict) -> float:
    """Насколько кадр годится в начало ролика: качество съёмки по LLM (0–10), при равенстве — красота CLIP.
    Кадр без оценки LLM — как средний (5)."""
    q = item.get("quality")
    return (float(q) if isinstance(q, (int, float)) else 5.0) + float(item.get("beauty") or 0) * 20


def hook_first(
    beauty: list[float],
    durations: list[float],
    seconds: float,
    *,
    video: list[bool] | None = None,
    allowed: list[bool] | None = None,
) -> list[int]:
    """Кадры на первые seconds ролика: сначала видео (красивейшие), потом фото — по остатку.

    allowed=False — кадр в начало не ставить (например, AI-кадр: в первые минуты — только живые).
    """
    if seconds <= 0:
        return []
    vid = video or [False] * len(beauty)
    ok = allowed or [True] * len(beauty)
    out: list[int] = []
    t = 0.0
    for i in sorted((i for i in range(len(beauty)) if ok[i]), key=lambda i: (not vid[i], -beauty[i])):
        if t >= seconds:
            break
        out.append(i)
        t += max(0.0, durations[i])
    return out
