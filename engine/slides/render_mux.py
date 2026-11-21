"""Микс голоса + фоновая музыка (duck) + опционально burn-in SRT."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from engine.slides.ffmpeg_util import find_ffmpeg, has_filter
from engine.slides.ffprobe_util import has_audio_stream, probe_duration, probe_video_size, run_ffmpeg
from engine.core.logutil import log
from engine.slides.look_presets import subs_force_style
from engine.slides.render_defaults import AUDIO_BITRATE, MUSIC_VOLUME, VIDEO_CRF, VIDEO_PRESET, rate_cap


def mux_av(
    *,
    video: Path,
    voice: Path,
    dest: Path,
    music: Path | None = None,
    music_volume: float = MUSIC_VOLUME,
    srt: Path | None = None,
) -> Path:
    ffmpeg = find_ffmpeg()
    duration = probe_duration(voice)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [ffmpeg, "-y", "-i", str(video), "-i", str(voice)]
    filters: list[str] = []
    audio_map = "1:a"
    video_map = "0:v"
    tmp_srt: Path | None = None
    soft_subs = False

    if music is not None and music.is_file() and has_audio_stream(music):
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(
            f"[2:a]volume={music_volume:.3f},"
            f"afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={max(0.0, duration - 2.5):.3f}:d=2.0[m]"
        )
        filters.append(
            "[1:a][m]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
        )
        audio_map = "[aout]"

    if srt is not None and srt.is_file():
        if not has_filter("subtitles"):
            # brew ffmpeg без libass — вшитые недоступны, кладём soft track
            log(
                "mux: нет фильтра subtitles (нужен ffmpeg с libass / brew ffmpeg-full). "
                "Субтитры будут soft (mov_text), не вшитые."
            )
            soft_subs = True
            cmd += ["-i", str(srt)]
        else:
            sub_vf, tmp_srt = subtitles_filter(srt)
            filters.append(f"[{video_map}]{sub_vf}[vout]")
            video_map = "[vout]"

    try:
        if filters:
            cmd += ["-filter_complex", ";".join(filters)]
            cmd += ["-map", video_map, "-map", audio_map]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]

        if soft_subs:
            # video=0, voice=1, [music=2], srt=last
            srt_idx = 3 if (music is not None and music.is_file() and has_audio_stream(music)) else 2
            cmd += ["-map", f"{srt_idx}:0", "-c:s", "mov_text"]

        if video_map == "0:v":
            # картинку не трогаем — копия потока, без лишнего пережатия
            cmd += ["-c:v", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF)]
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        run_ffmpeg(cmd, label="mux")
        return dest
    finally:
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)


def footage_subs_filter(srt: Path) -> tuple[str, Path]:
    """Фильтр вшивки SRT для футажей + временная копия SRT (удалить после ffmpeg).

    Стиль зависит от ролика (имя его папки): у разных роликов разные шрифты и цвета, у одного — всегда один.
    """
    style = subs_force_style(srt.parent.name)
    fd, tmp_name = tempfile.mkstemp(prefix="broll_subs_", suffix=".srt")
    os.close(fd)
    tmp_srt = Path(tmp_name)
    shutil.copy2(srt, tmp_srt)
    return f"subtitles={_escape_subtitles_path(tmp_srt)}:force_style='{style}'", tmp_srt


def burn_subtitles(video: Path, srt: Path) -> Path:
    """Вшить SRT в готовый mp4 (аудио без перекодирования если возможно)."""
    if not srt.is_file():
        return video
    ffmpeg = find_ffmpeg()
    tmp = video.with_name(video.stem + ".tmp_subs" + video.suffix)
    tmp_srt: Path | None = None
    try:
        if has_filter("subtitles"):
            vf, tmp_srt = footage_subs_filter(srt)
            size = probe_video_size(video)
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(video),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                VIDEO_PRESET,
                "-crf",
                str(VIDEO_CRF),
                *rate_cap(size[1] if size else 1080),
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        else:
            log(
                "burn_subtitles: нет фильтра subtitles — soft track (mov_text)"
            )
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(video),
                "-i",
                str(srt),
                "-c",
                "copy",
                "-c:s",
                "mov_text",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        run_ffmpeg(cmd, label="burn subs")
        tmp.replace(video)
        return video
    finally:
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def subtitles_filter(srt: Path) -> tuple[str, Path]:
    """Фильтр вшивания SRT (libass) и временная копия srt — её удалить после ffmpeg."""
    fd, tmp_name = tempfile.mkstemp(prefix="yvf_subs_", suffix=".srt")
    os.close(fd)
    tmp_srt = Path(tmp_name)
    shutil.copy2(srt, tmp_srt)
    esc = _escape_subtitles_path(tmp_srt)
    style = subs_force_style(srt.parent.name)  # свой стиль у каждого ролика
    return f"subtitles={esc}:force_style='{style}'", tmp_srt


def _escape_subtitles_path(path: Path) -> str:
    s = path.resolve().as_posix()
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", r"\'")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    return s
