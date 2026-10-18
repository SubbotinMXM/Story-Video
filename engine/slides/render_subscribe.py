"""Оверлей анимации подписки (chromakey) и блюр кадра на время плашки."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

import numpy as np

from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import has_audio_stream, probe_duration, run_ffmpeg
from engine.core.logutil import log
from engine.slides.render_mux import subtitles_filter
from engine.slides.render_defaults import (
    AUDIO_BITRATE,
    OUT_FPS,
    OUT_WIDTH,
    SUBSCRIBE_BLEND,
    SUBSCRIBE_BOTTOM_MARGIN,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_MAX_WIDTH_RATIO,
    SUBSCRIBE_SIMILARITY,
    SUBSCRIBE_START_MAX,
    SUBSCRIBE_START_MIN,
    SUBSCRIBE_VOLUME,
    VIDEO_CRF,
    VIDEO_PRESET,
)


# Кадр считаем «пустым», если почти весь он — фон хромакея.
_PROBE_FPS = 10
_PROBE_W, _PROBE_H = 160, 90
_KEY_DIST = 0.25  # доля от max RGB-расстояния: ближе — это фон
_VISIBLE_MIN = 0.005  # доля «не фоновых» пикселей, с которой плашка уже видна


def visible_span(sub_path: Path, sub_dur: float) -> tuple[float, float]:
    """Где в файле подписки реально что-то видно (секунды от начала файла).

    У анимаций часто зелёный хвост в 1–3 с: хромакей его вырезает, а блюр,
    посчитанный по длине файла, висел бы на пустом кадре.
    """
    try:
        raw = subprocess.run(
            [
                find_ffmpeg(),
                "-v", "error",
                "-i", str(sub_path),
                "-vf", f"fps={_PROBE_FPS},scale={_PROBE_W}:{_PROBE_H}",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        ).stdout
        frames = np.frombuffer(raw, np.uint8).reshape(-1, _PROBE_H, _PROBE_W, 3).astype(np.float32)
    except Exception as exc:
        log(f"Subscribe: не смог разобрать кадры ({exc}) — блюр на весь файл")
        return 0.0, sub_dur
    key = np.array([int(SUBSCRIBE_CHROMA[i : i + 2], 16) for i in (2, 4, 6)], np.float32)
    dist = np.sqrt(((frames - key) ** 2).sum(axis=-1)) / (255.0 * np.sqrt(3))
    visible = (dist > _KEY_DIST).mean(axis=(1, 2)) > _VISIBLE_MIN
    idx = np.flatnonzero(visible)
    if idx.size == 0:
        return 0.0, sub_dur
    lo = idx[0] / _PROBE_FPS
    hi = min(sub_dur, (idx[-1] + 1) / _PROBE_FPS)
    return float(lo), float(hi)


def subscribe_filters(
    *,
    start: float,
    sub_dur: float,
    sub_w: int,
    has_audio: bool,
    blur_span: tuple[float, float] | None = None,
    pre_vf: str = "",
    v_in: str = "0:v",
    a_in: str = "0:a",
    sub_in: int = 1,
) -> tuple[list[str], str]:
    """Блюр на весь кадр, пока плашка видна, поверх него сама анимация.

    blur_span — видимая часть анимации (секунды от её начала); без него
    блюр на всю длину файла.
    """
    end = start + sub_dur
    b_lo, b_hi = blur_span or (0.0, sub_dur)
    blur_start = start + b_lo
    blur_dur = max(0.2, b_hi - b_lo)
    fade = min(0.4, max(0.12, blur_dur / 4))
    fade_out = max(fade, blur_dur - fade)
    sub_y = f"H-h-{SUBSCRIBE_BOTTOM_MARGIN}"
    blur = (
        f"[forblur]trim=start={blur_start:.3f}:end={blur_start + blur_dur:.3f},setpts=PTS-STARTPTS,"
        f"boxblur=20:2,format=yuva420p,"
        f"fade=t=in:st=0:d={fade:.3f}:alpha=1,"
        f"fade=t=out:st={fade_out:.3f}:d={fade:.3f}:alpha=1,"
        f"setpts=PTS+{blur_start:.3f}/TB[blur]"
    )
    base = "[base][blur]overlay=format=auto:eof_action=pass[bg]"
    subv = (
        f"[{sub_in}:v]fps={OUT_FPS},"
        f"chromakey={SUBSCRIBE_CHROMA}:{SUBSCRIBE_SIMILARITY}:{SUBSCRIBE_BLEND},"
        f"format=yuva420p,"
        f"scale={sub_w}:-2:flags=lanczos,"
        f"setpts=PTS+{start:.3f}/TB[subv]"
    )
    overlay = (
        f"[bg][subv]overlay=(W-w)/2:{sub_y}:format=auto:"
        f"eof_action=pass:"
        f"enable='between(t\\,{start:.3f}\\,{end:.3f})'[vout]"
    )
    # pre_vf (субтитры) ложится под блюр и плашку — как при отдельном проходе
    head = f"[{v_in}]{pre_vf},split[base][forblur]" if pre_vf else f"[{v_in}]split[base][forblur]"
    filters = [head, blur, base, subv, overlay]
    if has_audio:
        delay_ms = int(start * 1000)
        filters += [
            f"[{a_in}]aformat=sample_fmts=fltp:channel_layouts=stereo[a0]",
            (
                f"[{sub_in}:a]volume={SUBSCRIBE_VOLUME},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms}[a1]"
            ),
            "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
        ]
        return filters, "[aout]"
    return filters, a_in if ":" in a_in else f"[{a_in}]"


def subscribe_start(seed: int | None, total: float, sub_dur: float) -> float:
    """Как в футажах: 4–5 с от начала готового ролика (короткий ролик — раньше)."""
    start = random.Random(seed).uniform(SUBSCRIBE_START_MIN, SUBSCRIBE_START_MAX)
    if start + 1.0 >= total:
        start = max(0.5, total * 0.15)
    if start + sub_dur > total - 0.2:
        start = max(0.3, total - sub_dur - 0.2)
    return start


def overlay_subscribe(
    video: Path,
    sub_path: Path,
    *,
    seed: int | None = None,
    run_dir: Path | None = None,
    srt: Path | None = None,
) -> bool:
    """srt — заодно вшить субтитры в этом же проходе (на одно пережатие меньше)."""
    if not sub_path.is_file():
        log(f"Subscribe: файл не найден, пропускаю ({sub_path})")
        return False

    ffmpeg = find_ffmpeg()
    total = probe_duration(video)
    sub_dur = probe_duration(sub_path)
    start = subscribe_start(seed, total, sub_dur)
    why = "4–5с"
    sub_w = int(OUT_WIDTH * SUBSCRIBE_MAX_WIDTH_RATIO)
    span = visible_span(sub_path, sub_dur)
    log(f"Subscribe: плашка видна {span[0]:.1f}–{span[1]:.1f}с из {sub_dur:.1f}с — блюр только на это время")
    pre_vf, tmp_srt = subtitles_filter(srt) if srt is not None and srt.is_file() else ("", None)
    filters, audio_map = subscribe_filters(
        start=start,
        sub_dur=sub_dur,
        sub_w=sub_w,
        has_audio=has_audio_stream(sub_path),
        blur_span=span,
        pre_vf=pre_vf,
    )

    tmp = video.with_name(video.stem + ".tmp_sub" + video.suffix)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-i",
        str(sub_path),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        audio_map,
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    try:
        run_ffmpeg(cmd, label="subscribe")
        tmp.replace(video)
        log(f"Subscribe: {why} @ {start:.1f}s")
        return True
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)
