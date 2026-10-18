"""Параметры рендера 16:9 1080p."""

from __future__ import annotations

OUT_WIDTH = 1920
OUT_HEIGHT = 1080
OUT_FPS = 30
VIDEO_PRESET = "veryfast"
VIDEO_CRF = 20  # плотнее для ≤5ГБ на длинных
AUDIO_BITRATE = "192k"
MUSIC_VOLUME = 0.12
SUBSCRIBE_VOLUME = 0.8
SUBSCRIBE_CHROMA = "0x60FB01"
SUBSCRIBE_SIMILARITY = 0.12
SUBSCRIBE_BLEND = 0.08
SUBSCRIBE_MAX_WIDTH_RATIO = 0.55
SUBSCRIBE_BOTTOM_MARGIN = 48
SUBSCRIBE_START_MIN = 4.0
SUBSCRIBE_START_MAX = 5.0


def rate_cap(height: int) -> list[str]:
    """Потолок битрейта: обычный ролик под него не упирается, шумный — не раздувается.

    720p: 3 Мбит/с (~1,1 ГБ на 49 мин в худшем случае; обычные футажи ~2–2,5 Мбит/с).
    1080p: 8 Мбит/с — рекомендация YouTube для 1080p30.
    """
    rate = 3 if height <= 720 else 8
    return ["-maxrate", f"{rate}M", "-bufsize", f"{rate * 2}M"]
