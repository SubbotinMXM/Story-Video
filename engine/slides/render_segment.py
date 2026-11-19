"""Сегмент: видео и картинка на 16:9 с блюром по краям."""

from __future__ import annotations

from pathlib import Path

from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import probe_duration, probe_video_size, run_ffmpeg
from engine.slides.render_defaults import (
    OUT_FPS,
    OUT_HEIGHT,
    OUT_WIDTH,
    VIDEO_CRF,
    VIDEO_PRESET,
)

# zoompan округляет x/y до целых пикселей. На 1080p это шаг в 1px и видимая тряска.
# Считаем движение на 2× кадре: шаг — полпикселя на выходе. По замеру дрожание движения
# 0,16 px против 0,14 px на 4× (глазом не видно), а кадр кодируется почти вдвое быстрее
# (2,5 с против 4,6 с на фото) — ролик из 700 фото собирается на ~6 минут быстрее.
# Сам zoompan отдаёт сразу 1080p (а не 4K с последующим уменьшением): та же картинка (замер на 6 фото против
# эталона на 4× кадре: SSIM 0,9816 и PSNR 41,24 дБ — как у прежней схемы 0,9817 и 41,25 дБ), сегмент на 39% быстрее.
_MOTION_SUPER = 2


def render_video_cover(
    *,
    media: Path,
    duration: float,
    dest: Path,
) -> Path:
    """Любой аспект → 1920x1080: ролик вписан, пустые края закрыты блюром."""
    if duration <= 0.05:
        duration = 0.05
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    w, h, fps = OUT_WIDTH, OUT_HEIGHT, OUT_FPS
    src_dur = probe_duration(media) if media.is_file() else 0.0
    inputs = (
        ["-stream_loop", "-1", "-i", str(media)]
        if src_dur + 0.05 < duration
        else ["-i", str(media)]
    )
    # out_range=tv: все сегменты в одном цветовом диапазоне — иначе склейка «меняет формат на ходу»
    tail = f"fps={fps},scale=out_range=tv,format=yuv420p,trim=duration={duration:.3f},setpts=PTS-STARTPTS"
    size = probe_video_size(media) if media.is_file() else None
    if size is not None and size[0] * h == size[1] * w:
        # Ровно 16:9: ролик закрывает кадр целиком, размытый фон под ним
        # не виден — не считаем его. Пиксели те же, что с фоном.
        vf = f"scale={w}:{h}:flags=lanczos,{tail}"
    else:
        vf = (
            f"split[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=24:8[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
            f"{tail}"
        )
    cmd = [
        ffmpeg,
        "-y",
        *inputs,
        "-filter_complex",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-pix_fmt",
        "yuv420p",
        str(dest),
    ]
    run_ffmpeg(cmd, label=f"seg-video {dest.name}")
    return dest


# переходы между фото: calm — уход в чёрный (как было), flash — короткая светлая вспышка вместо чёрного.
# Цвет вспышки чередуется (белый / тёплый свет) и общий для стыка: уходящий кадр гаснет в тот же цвет,
# из которого появляется следующий — поэтому цвет зависит от номера стыка (seed идёт подряд).
TRANSITIONS = ("calm", "flash")
_FLASH_COLORS = ("white", "0xFFE9C4")
FLASH_IN_SEC = 0.22
FLASH_OUT_SEC = 0.16


def transition_fades(style: str, seed: int, duration: float, fade_sec: float) -> tuple[float, float, str, str]:
    """(длина появления, длина ухода, цвет появления, цвет ухода) для фото."""
    if style == "flash":
        cap = max(0.05, duration / 3.0)
        return (min(FLASH_IN_SEC, cap), min(FLASH_OUT_SEC, cap),
                _FLASH_COLORS[(seed - 1) % len(_FLASH_COLORS)], _FLASH_COLORS[seed % len(_FLASH_COLORS)])
    half = max(0.05, min(fade_sec / 2.0, duration / 3.0))
    return half, half, "black", "black"


def image_motion_filter(
    *,
    duration: float,
    seed: int,
    fade_sec: float,
    w: int,
    h: int,
    fps: int,
    transition: str = "calm",
) -> tuple[str, int]:
    """Один кадр → плавный зум или панорама. Вход в zoompan крупный, иначе он дёргается."""
    frames = max(2, int(round(duration * fps)))
    den = frames - 1
    fade_in, fade_out, color_in, color_out = transition_fades(transition, seed, duration, fade_sec)
    fade_out_st = max(0.0, duration - fade_out)
    super_w, super_h = w * _MOTION_SUPER, h * _MOTION_SUPER
    mode = seed % 3
    if mode == 0:
        z = f"1+on*0.08/{den}"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif mode == 1:
        z = "1.10"
        x = f"(iw-iw/zoom)*on/{den}"
        y = "ih/2-(ih/zoom/2)"
    else:
        z = "1.10"
        x = f"(iw-iw/zoom)*(1-on/{den})"
        y = "ih/2-(ih/zoom/2)"
    vf = (
        f"split[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=24:8[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        f"scale={super_w}:{super_h}:flags=lanczos+accurate_rnd,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},"
        f"fade=t=in:st=0:d={fade_in:.3f}:color={color_in},"
        f"fade=t=out:st={fade_out_st:.3f}:d={fade_out:.3f}:color={color_out},"
        # JPEG — полный диапазон (yuvj420p); приводим к обычному, как у видео
        f"scale=out_range=tv,format=yuv420p"
    )
    return vf, frames


def render_image_canvas(
    *,
    media: Path,
    duration: float,
    dest: Path,
    seed: int = 0,
    fade_sec: float = 0.5,
    transition: str = "calm",
) -> Path:
    """Картинка на 16:9: блюр по краям, плавный зум или панорама, без рывков."""
    if duration <= 0.05:
        duration = 0.05
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    w, h, fps = OUT_WIDTH, OUT_HEIGHT, OUT_FPS
    vf, frames = image_motion_filter(
        duration=duration, seed=seed, fade_sec=fade_sec, w=w, h=h, fps=fps, transition=transition
    )
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(media),
        "-filter_complex",
        vf,
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-pix_fmt",
        "yuv420p",
        str(dest),
    ]
    run_ffmpeg(cmd, label=f"seg-still {dest.name}")
    return dest
