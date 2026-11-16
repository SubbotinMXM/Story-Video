"""Константы и дефолты композера."""

from __future__ import annotations

from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

CORNERS = ("tl", "tr", "bl", "br")

# 720p 16:9 — не ниже: интро/аутро и футажи иначе выходят пиксельными
OUT_WIDTH = 1280
OUT_HEIGHT = 720
OUT_FPS = 30
# интро/аутро не уменьшают ролик ниже этого (стоки — 1080p, футажи — 720p)
MIN_OUT_HEIGHT = 720

# сколько случайных футажей на ролик; рассказ делится на них поровну
BROLL_COUNT = 8

HEAD_WIDTH_RATIO = 0.20
# размеры подписи и отступов — в той же пропорции к кадру, что были при 480p
OVERLAY_MARGIN = 24

TEXT_FONTSIZE = 84

VIDEO_PRESET = "veryfast"
VIDEO_CRF = 23  # исходные футажи 480p: сжатие сильнее не даёт видимой разницы, а файл в 1,5 раза легче
AUDIO_BITRATE = "192k"

# Анимация подписки (хромакей). Клади файл сюда или передай путь в ComposeRequest;
# при subscribe=True пайплайн проверяет is_file.
SUBSCRIBE_PATH = str(
    Path(__file__).resolve().parents[2] / "assets" / "subscribe.mp4"
)
SUBSCRIBE_VOLUME = 0.8  # на 20% тише оригинала
SUBSCRIBE_CHROMA = "0x60FB01"  # реальный зелёный из AAA.mp4
SUBSCRIBE_SIMILARITY = 0.12
SUBSCRIBE_BLEND = 0.08
# После crop по контенту — почти на всю ширину кадра.
SUBSCRIBE_MAX_WIDTH_RATIO = 0.85
# Отступ плашки подписки от нижнего края кадра (не по центру).
SUBSCRIBE_BOTTOM_MARGIN = 0

MAC_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
