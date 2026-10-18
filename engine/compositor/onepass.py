"""Один проход ffmpeg на весь ролик: интро + рассказ + аутро в одном filter_complex.

Раньше интро, аутро, субтитры и подписка шли отдельными пережатиями всего ролика —
каждое заново кодировало час видео (время, место на диске, потеря качества).
Здесь только помощники для графа; сами графы собирают футажи и слайды.
"""

from __future__ import annotations

from pathlib import Path

from engine.compositor.utils import has_audio_stream, probe_duration

AUDIO_FMT = "aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000"


class Inputs:
    """Входы ffmpeg с их номерами: добавляешь — получаешь индекс."""

    def __init__(self, first_index: int = 0) -> None:
        self.args: list[str] = []
        self.next = first_index

    def add(self, *args: str) -> int:
        self.args += list(args)
        idx = self.next
        self.next += 1
        return idx

    def silence(self, duration: float) -> str:
        idx = self.add("-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000")
        return f"{idx}:a"

    def clip(self, path: Path) -> tuple[int, str, float]:
        """Клип со звуком (или тишиной той же длины) → (индекс видео, вход аудио, длительность)."""
        dur = probe_duration(path)
        idx = self.add("-i", str(path))
        audio = f"{idx}:a" if has_audio_stream(path) else self.silence(dur)
        return idx, audio, dur


def edge_filters(idx: int, audio: str, dur: float, *, size: tuple[int, int], fps: int, label: str) -> list[str]:
    """Интро/аутро под размер ролика: [label_v] и [label_a] ровно длиной dur."""
    w, h = size
    return [
        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p,"
        f"trim=duration={dur:.3f},setpts=PTS-STARTPTS[{label}_v]",
        f"[{audio}]{AUDIO_FMT},apad,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[{label}_a]",
    ]


def story_audio(audio: str, dur: float, label: str = "story_a") -> str:
    """Звук рассказа ровно длиной картинки — иначе аутро съедет относительно звука."""
    return f"[{audio}]{AUDIO_FMT},apad,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[{label}]"


def join(parts: list[str], *, out_v: str = "cat_v", out_a: str = "cat_a") -> list[str]:
    """Склейка частей «[x_v][x_a]» в [out_v][out_a]."""
    if len(parts) == 1:
        v, a = parts[0].split("][")
        return [f"{v}]null[{out_v}]", f"[{a}anull[{out_a}]"]
    return ["".join(parts) + f"concat=n={len(parts)}:v=1:a=1[{out_v}][{out_a}]"]
