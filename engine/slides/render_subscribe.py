"""Анимация подписки в стоковых роликах: 3 показа внизу кадра, без блюра (общий граф — compositor.subscribe)."""

from __future__ import annotations

import random
from pathlib import Path

from engine.compositor.subscribe import subscribe_graph, subscribe_starts
from engine.compositor.utils import probe_chromakey_crop
from engine.slides.ffprobe_util import has_audio_stream, probe_duration
from engine.slides.render_defaults import (
    OUT_FPS,
    OUT_WIDTH,
    SUBSCRIBE_BLEND,
    SUBSCRIBE_BOTTOM_MARGIN,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_MAX_WIDTH_RATIO,
    SUBSCRIBE_SIMILARITY,
    SUBSCRIBE_VOLUME,
)


def subscribe_filters(
    sub_path: Path,
    sub_idx: int,
    total: float,
    *,
    seed: int | None = None,
    v_in: str = "cat_v",
    a_in: str = "cat_a",
) -> tuple[list[str], list[float]]:
    """Граф подписки поверх [v_in]/[a_in] → [vout]/[aout] и моменты показов."""
    sub_dur = probe_duration(sub_path)
    starts = subscribe_starts(total, sub_dur, random.Random(seed))
    crop = probe_chromakey_crop(
        sub_path, chroma=SUBSCRIBE_CHROMA, similarity=SUBSCRIBE_SIMILARITY, blend=SUBSCRIBE_BLEND, pad=8
    )
    filters = subscribe_graph(
        sub_idx,
        has_audio_stream(sub_path),
        starts=starts,
        sub_dur=sub_dur,
        crop=crop,
        width=int(OUT_WIDTH * SUBSCRIBE_MAX_WIDTH_RATIO),
        margin=SUBSCRIBE_BOTTOM_MARGIN,
        fps=OUT_FPS,
        chroma=SUBSCRIBE_CHROMA,
        similarity=SUBSCRIBE_SIMILARITY,
        blend=SUBSCRIBE_BLEND,
        volume=SUBSCRIBE_VOLUME,
        v_in=v_in,
        a_in=a_in,
    )
    return filters, starts
