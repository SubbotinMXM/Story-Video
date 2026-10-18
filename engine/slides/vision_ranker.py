"""Локальный отсев кадров через OpenCLIP (без LLM vision)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.slides.clip_ranker import (
    DEFAULT_THRESHOLD,
    score_image_paths,
    score_image_paths_contextual,
)

# Мягче прежнего LLM-judge: mid CLIP уже «нормально».
_SOFT_FLOOR = 0.10  # ниже — почти мусор
_GOOD = 0.18  # уверенный матч (раньше ~threshold 0.20)


def score_fit(
    client: Any,
    path: Path,
    *,
    narration: str,
    query: str,
    era: str,
    place: str,
    historical: bool,
) -> tuple[int, str]:
    """client игнорируется (совместимость вызовов). Возвращает fit 0–10 + reason."""
    _ = client
    pos = _positive_prompt(query=query, narration=narration, era=era, place=place)
    if not pos:
        return 3, "empty prompt"
    if historical:
        neg = _negative_prompt(era=era, place=place)
        ranked = score_image_paths_contextual(
            pos, [path], neg_query=neg, neg_weight=0.7, threshold=DEFAULT_THRESHOLD
        )
    else:
        ranked = score_image_paths(pos, [path], threshold=DEFAULT_THRESHOLD)
    if not ranked:
        return 0, "clip failed"
    _p, sim = ranked[0]
    fit = _cosine_to_fit(float(sim), soft=not historical)
    reason = f"clip={float(sim):.3f}"
    return fit, reason


def _positive_prompt(*, query: str, narration: str, era: str, place: str) -> str:
    """Только английский: CLIP (ViT-B-32/LAION) другие языки не понимает.

    narration на языке ролика сюда не идёт — по замеру он только шум; смысл фразы
    уже переведён в английский поисковый запрос (его пишет LLM под каждый кусок).
    """
    _ = narration
    parts = [(query or "").strip(), (place or "").strip(), (era or "").strip()]
    return ", ".join(p for p in parts if p)


def _negative_prompt(*, era: str, place: str) -> str:
    parts = [
        "modern contemporary lifestyle smartphone jeans sneakers highway neon skyline",
        "tourist selfie cruise ship container port supermarket yogurt",
    ]
    if era:
        parts.append(f"anachronistic not {era}")
    if place:
        parts.append(f"wrong place not {place}")
    return ", ".join(parts)


def _cosine_to_fit(sim: float, *, soft: bool) -> int:
    """CLIP cosine → 0..10. soft=True чуть выше оценки (менее требовательно)."""
    # Типичный диапазон OpenCLIP ViT-B-32: 0.08…0.32
    lo = _SOFT_FLOOR
    hi = 0.30 if soft else 0.28
    if sim <= lo:
        return 2 if soft else 1
    if sim >= hi:
        return 9 if soft else 8
    # линейно lo→3, hi→9 (soft) / 8
    top = 9.0 if soft else 8.0
    t = (sim - lo) / (hi - lo)
    fit = 3.0 + t * (top - 3.0)
    # бонус за «хороший» диапазон
    if soft and sim >= _GOOD:
        fit = min(10.0, fit + 0.5)
    return max(0, min(10, int(round(fit))))
