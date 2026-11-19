"""Сборка FFmpeg filter_complex и запуск рендера."""

from __future__ import annotations

import random
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engine.compositor.defaults import (
    AUDIO_BITRATE,
    HEAD_WIDTH_RATIO,
    MIN_OUT_HEIGHT,
    OUT_FPS,
    OUT_HEIGHT,
    OUT_WIDTH,
    OVERLAY_MARGIN,
    SUBSCRIBE_BLEND,
    SUBSCRIBE_BOTTOM_MARGIN,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_MAX_WIDTH_RATIO,
    SUBSCRIBE_PATH,
    SUBSCRIBE_SIMILARITY,
    SUBSCRIBE_VOLUME,
    VIDEO_CRF,
    VIDEO_PRESET,
)
from engine.core.cancel import watch_process
from engine.slides.ffmpeg_util import low_priority
from engine.slides.ffprobe_util import disk_full_hint
from engine.slides.render_defaults import rate_cap
from engine.compositor.onepass import Inputs, edge_filters, join, story_audio
from engine.compositor.picker import pick_broll, pick_head, pick_intro, pick_outro
from engine.compositor.text_overlay import render_text_png
from engine.compositor.subscribe import subscribe_graph, subscribe_starts
from engine.compositor.utils import (
    find_ffmpeg,
    has_audio_stream,
    probe_chromakey_crop,
    probe_duration,
)


@dataclass
class ComposeRequest:
    audio: Path
    broll_dir: Path
    head_dir: Path | None
    text: str
    output: Path
    seed: int | None = None
    subscribe: bool = False
    subscribe_path: Path | None = Path(SUBSCRIBE_PATH)
    intro_dir: Path | None = None
    outro_dir: Path | None = None
    # вшить субтитры в этом же проходе (без второго полного пережатия ролика)
    srt: Path | None = None


@dataclass
class ComposeResult:
    output: Path
    duration: float
    broll: list[Path]
    head: Path | None
    head_corner: str
    text_corner: str
    subscribe: bool
    intro: Path | None = None
    outro: Path | None = None


ProgressCb = Callable[[float, str], None]


def _corner_xy(corner: str, margin: int) -> tuple[str, str]:
    mapping = {
        "tl": (f"{margin}", f"{margin}"),
        "tr": (f"W-w-{margin}", f"{margin}"),
        "bl": (f"{margin}", f"H-h-{margin}"),
        "br": (f"W-w-{margin}", f"H-h-{margin}"),
    }
    return mapping[corner]


# голова диктора — всегда справа сверху, надпись — всегда слева сверху
HEAD_CORNER = "tr"
TEXT_CORNER = "tl"


def _scale_crop(label_in: str, label_out: str) -> str:
    w, h, fps = OUT_WIDTH, OUT_HEIGHT, OUT_FPS
    return (
        f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[{label_out}]"
    )


def build_and_run(
    req: ComposeRequest,
    on_progress: ProgressCb | None = None,
) -> ComposeResult:
    rng = random.Random(req.seed)
    ffmpeg = find_ffmpeg()
    duration = probe_duration(req.audio)

    broll = pick_broll(req.broll_dir, rng)
    head = pick_head(req.head_dir, rng)
    use_head = head is not None
    head_corner, text_corner = HEAD_CORNER, TEXT_CORNER

    n = len(broll)
    slot = duration / n
    head_w = int(OUT_WIDTH * HEAD_WIDTH_RATIO)

    use_sub = req.subscribe
    sub_path = req.subscribe_path
    sub_dur = 0.0
    sub_crop: tuple[int, int, int, int] | None = None
    if use_sub:
        if sub_path is None or not sub_path.is_file():
            use_sub = False
            msg = f"Подписка: файл не найден, пропускаю ({sub_path})"
            print(f"[WARN] {msg}")
            if on_progress:
                on_progress(0.0, msg)
        else:
            sub_dur = probe_duration(sub_path)
            sub_crop = probe_chromakey_crop(
                sub_path,
                chroma=SUBSCRIBE_CHROMA,
                similarity=SUBSCRIBE_SIMILARITY,
                blend=SUBSCRIBE_BLEND,
                pad=8,
            )

    req.output.parent.mkdir(parents=True, exist_ok=True)
    use_text = bool((req.text or "").strip())
    text_png = req.output.with_suffix(".text.png")
    if use_text:
        render_text_png(req.text, text_png, text_corner)

    # inputs:
    # 0=audio
    # 1..n=broll
    # n+1[+head?]=head (optional)
    # next=text png (optional)
    cmd: list[str] = [ffmpeg, "-y", "-i", str(req.audio)]
    for clip in broll:
        cmd += ["-stream_loop", "-1", "-t", f"{slot:.3f}", "-i", str(clip)]
    if use_head:
        cmd += ["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", str(head)]
    if use_text:
        cmd += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(text_png)]

    filters: list[str] = []
    concat_inputs: list[str] = []

    for i in range(n):
        inp = f"{i + 1}:v"
        scaled = f"b{i}"
        trimmed = f"bt{i}"
        filters.append(_scale_crop(inp, scaled))
        filters.append(
            f"[{scaled}]trim=duration={slot:.3f},setpts=PTS-STARTPTS[{trimmed}]"
        )
        concat_inputs.append(f"[{trimmed}]")

    filters.append("".join(concat_inputs) + f"concat=n={n}:v=1:a=0[bg0]")
    # футажи — как лежат в папке, без цветовых эффектов и зерна
    filters.append("[bg0]null[bg]")

    base_label = "withtext"
    if use_head:
        head_in = f"{n + 1}:v"
        filters.append(
            f"[{head_in}]scale={head_w}:-2,setsar=1,fps={OUT_FPS},format=yuv420p,"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[head]"
        )
        ox, oy = _corner_xy(head_corner, OVERLAY_MARGIN)
        filters.append(f"[bg][head]overlay=x={ox}:y={oy}:format=auto[withhead]")
    else:
        filters.append("[bg]null[withhead]")

    if use_text:
        text_in_idx = n + 1 + (1 if use_head else 0)
        text_in = f"{text_in_idx}:v"
        filters.append(
            f"[{text_in}]format=rgba,fps={OUT_FPS}[txt];"
            f"[withhead][txt]overlay=0:0:format=auto[{base_label}]"
        )
    else:
        filters.append(f"[withhead]null[{base_label}]")
    tmp_srt: Path | None = None
    if req.srt is not None and req.srt.is_file():
        from engine.slides.render_mux import footage_subs_filter

        subs_vf, tmp_srt = footage_subs_filter(req.srt)
        filters.append(f"[{base_label}]{subs_vf}[story_v]")
    else:
        filters.append(f"[{base_label}]null[story_v]")

    # Интро, рассказ, аутро и подписка — в этом же проходе: ролик кодируется один раз.
    inputs = Inputs(first_index=1 + n + (1 if use_head else 0) + (1 if use_text else 0))
    parts: list[str] = []
    intro_path = pick_intro(req.intro_dir, rng) if req.intro_dir is not None and req.intro_dir.is_dir() else None
    outro_path = pick_outro(req.outro_dir, rng) if req.outro_dir is not None and req.outro_dir.is_dir() else None
    total = duration
    size = (OUT_WIDTH, OUT_HEIGHT)
    if intro_path is not None:
        idx, audio, dur = inputs.clip(intro_path)
        filters += edge_filters(idx, audio, dur, size=size, fps=OUT_FPS, label="intro")
        parts.append("[intro_v][intro_a]")
        total += dur
    filters.append(story_audio("0:a", duration))
    parts.append("[story_v][story_a]")
    if outro_path is not None:
        idx, audio, dur = inputs.clip(outro_path)
        filters += edge_filters(idx, audio, dur, size=size, fps=OUT_FPS, label="outro")
        parts.append("[outro_v][outro_a]")
        total += dur
    filters += join(parts)

    # Подписка — поверх всего ролика (интро+рассказ+аутро): ~25 с, середина, за 30 с до конца.
    if use_sub and sub_path is not None:
        sub_idx = inputs.add("-i", str(sub_path))
        starts = subscribe_starts(total, sub_dur, rng)
        filters += _subscribe_graph(sub_idx, has_audio_stream(sub_path), starts=starts, sub_dur=sub_dur, crop=sub_crop)
        if on_progress:
            crop_s = f"crop={sub_crop[2]}x{sub_crop[3]}" if sub_crop else "без crop"
            when = ", ".join(f"{t:.0f}с" for t in starts)
            on_progress(0.0, f"Подписка: {sub_path.name}, показы на {when} ({crop_s})")
    else:
        filters += ["[cat_v]null[vout]", "[cat_a]anull[aout]"]
    cmd += inputs.args

    filter_complex = ";".join(filters)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        *rate_cap(OUT_HEIGHT),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-t",
        f"{total:.3f}",
        "-movflags",
        "+faststart",
        str(req.output),
    ]

    if on_progress:
        on_progress(0.0, "Старт рендера…")

    try:
        _run_ffmpeg(cmd, total, on_progress)
    finally:
        if text_png.exists():
            text_png.unlink(missing_ok=True)
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)

    return ComposeResult(
        output=req.output,
        duration=duration,
        broll=broll,
        head=head,
        head_corner=head_corner,
        text_corner=text_corner,
        subscribe=use_sub,
        intro=intro_path,
        outro=outro_path,
    )


def _subscribe_graph(
    sub_idx: int,
    sub_audio: bool,
    *,
    starts: list[float],
    sub_dur: float,
    crop: tuple[int, int, int, int] | None,
    v_in: str = "cat_v",
    a_in: str = "cat_a",
) -> list[str]:
    """Анимация подписки (хромакей) поверх [v_in]/[a_in] → [vout]/[aout] в моменты starts."""
    return subscribe_graph(
        sub_idx,
        sub_audio,
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


def _overlay_subscribe(
    video: Path,
    sub_path: Path,
    *,
    crop: tuple[int, int, int, int] | None,
    on_progress: ProgressCb | None,
    rng: random.Random | None = None,
) -> None:
    """Вклеить анимацию подписки поверх готового ролика: ~25 с, середина, за 30 с до конца."""
    ffmpeg = find_ffmpeg()
    total = probe_duration(video)
    sub_dur = probe_duration(sub_path)
    starts = subscribe_starts(total, sub_dur, rng)
    cmd: list[str] = [ffmpeg, "-y", "-i", str(video), "-i", str(sub_path)]
    filters = _subscribe_graph(1, has_audio_stream(sub_path), starts=starts, sub_dur=sub_dur, crop=crop, v_in="0:v", a_in="0:a")
    audio_map = "[aout]"

    tmp = video.with_name(video.stem + ".tmp_sub" + video.suffix)
    cmd += [
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
        *rate_cap(OUT_HEIGHT),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    try:
        _run_ffmpeg(cmd, total, on_progress)
        tmp.replace(video)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _maybe_prepend_intro(
    main: Path,
    intro_dir: Path | None,
    *,
    rng: random.Random,
    on_progress: ProgressCb | None,
) -> Path | None:
    """Если папка интро задана — вклеить случайный клип в начало. Иначе no-op."""
    if intro_dir is None:
        return None
    if not intro_dir.is_dir():
        if on_progress:
            on_progress(0.90, f"Интро: папка не найдена, пропускаю ({intro_dir})")
        return None
    clip = pick_intro(intro_dir, rng)
    if clip is None:
        if on_progress:
            on_progress(0.90, f"Интро: нет видео в папке, пропускаю ({intro_dir})")
        return None
    if on_progress:
        on_progress(0.90, f"Интро: {clip.name}")
    tmp = main.with_name(main.stem + ".tmp_intro" + main.suffix)
    try:
        _concat_videos(clip, main, tmp, on_progress=on_progress, size=concat_size(main))
        tmp.replace(main)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    if on_progress:
        on_progress(0.92, "Интро: готово")
    return clip


def _maybe_append_outro(
    main: Path,
    outro_dir: Path | None,
    *,
    rng: random.Random,
    on_progress: ProgressCb | None,
) -> Path | None:
    """Если папка аутро задана — дописать случайный клип в конец. Иначе no-op."""
    if outro_dir is None:
        return None
    if not outro_dir.is_dir():
        if on_progress:
            on_progress(1.0, f"Аутро: папка не найдена, пропускаю ({outro_dir})")
        return None
    clip = pick_outro(outro_dir, rng)
    if clip is None:
        if on_progress:
            on_progress(1.0, f"Аутро: нет видео в папке, пропускаю ({outro_dir})")
        return None
    if on_progress:
        on_progress(0.93, f"Аутро: {clip.name}")
    tmp = main.with_name(main.stem + ".tmp_outro" + main.suffix)
    try:
        _concat_videos(main, clip, tmp, on_progress=on_progress, size=concat_size(main))
        tmp.replace(main)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    if on_progress:
        on_progress(1.0, "Аутро: готово")
    return clip


def concat_size(main: Path) -> tuple[int, int]:
    """Размер склейки = размер основного ролика (1080p остаётся 1080p), но не ниже 720p."""
    from engine.slides.ffprobe_util import probe_video_size

    size = probe_video_size(main)
    w, h = size if size else (OUT_WIDTH, OUT_HEIGHT)
    if h < MIN_OUT_HEIGHT:
        w, h = round(w * MIN_OUT_HEIGHT / h), MIN_OUT_HEIGHT
    return w - w % 2, h - h % 2


def _concat_videos(
    first: Path,
    second: Path,
    output: Path,
    *,
    on_progress: ProgressCb | None,
    size: tuple[int, int] | None = None,
) -> None:
    """Склеить first + second (scale/crop как у футажей) в output размером size."""
    ffmpeg = find_ffmpeg()
    first_dur = probe_duration(first)
    second_dur = probe_duration(second)
    total = first_dur + second_dur
    w, h = size or (OUT_WIDTH, OUT_HEIGHT)
    fps = OUT_FPS
    first_a = has_audio_stream(first)
    second_a = has_audio_stream(second)

    cmd: list[str] = [ffmpeg, "-y", "-i", str(first), "-i", str(second)]
    # Индексы: 0=first, 1=second, далее опциональные anullsrc.
    next_in = 2
    a0_src = "0:a"
    a1_src = "1:a"
    if not first_a:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{first_dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        a0_src = f"{next_in}:a"
        next_in += 1
    if not second_a:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{second_dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        a1_src = f"{next_in}:a"

    filters = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v0];"
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v1];"
        f"[{a0_src}]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a0];"
        f"[{a1_src}]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
    )

    cmd += [
        "-filter_complex",
        filters,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        *rate_cap(h),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, total, on_progress)


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _parse_time(s: str) -> float | None:
    m = _TIME_RE.search(s)
    if not m:
        return None
    h, mi, sec = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(sec)


def _run_ffmpeg(
    cmd: list[str],
    total: float,
    on_progress: ProgressCb | None,
) -> None:
    proc = subprocess.Popen(
        low_priority(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    err_lines: list[str] = []
    with watch_process(proc):  # «Стоп» убивает ffmpeg сразу — сборка рассказа идёт десятки минут
        for line in proc.stderr:
            err_lines.append(line)
            if on_progress and total > 0:
                t = _parse_time(line)
                if t is not None:
                    pct = max(0.0, min(0.99, t / total))
                    on_progress(pct, f"Рендер {pct * 100:.0f}%")
        code = proc.wait()
    if code != 0:
        tail = "".join(err_lines[-40:])
        hint = disk_full_hint(cmd, tail)
        if hint:
            raise RuntimeError(hint)
        raise RuntimeError(f"ffmpeg failed ({code}):\n{tail}")
    if on_progress:
        on_progress(1.0, "Готово")
