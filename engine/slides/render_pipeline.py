"""Рендер: timeline + voice → mp4 на Desktop."""

from __future__ import annotations

import json
import math
import random
import shutil
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.core.spread import spread
from engine.slides.music import MUSIC_VOLUME
from engine.slides.hook import HOOK_SEC, head_rank, hook_first
from engine.slides.look import look_groups
from engine.core.throttle import heavy_stage
from engine.core.threads import ThreadPoolExecutor
from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken
from engine.slides.ffprobe_util import concat_demuxer, probe_duration
from engine.core.logutil import log
from engine.slides.paths import AUDIO_NAME, TIMELINE_NAME, TIMINGS_NAME
from engine.slides.publish import export_ready_folder, write_credits
from engine.slides.speed_copies import write_speed_copies
from engine.slides.ffmpeg_util import has_filter
from engine.slides.render_mux import mux_av
from engine.slides.render_segment import render_image_canvas, render_video_cover
from engine.slides.render_subscribe import subscribe_filters
from engine.slides.render_mux import subtitles_filter
from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import has_audio_stream, run_ffmpeg
from engine.slides.render_defaults import (
    AUDIO_BITRATE,
    OUT_FPS,
    OUT_HEIGHT,
    OUT_WIDTH,
    VIDEO_CRF,
    VIDEO_PRESET,
)
from engine.slides.subtitles import timings_to_srt

ProgressCb = Callable[[float, str], None]


SILENT_NAME = "video_silent.mp4"

# сегментов рендерится одновременно (каждый ffmpeg и сам многопоточный)
RENDER_WORKERS = 4


@dataclass
class RenderRequest:
    run_dir: Path
    subscribe: bool = False
    subscribe_path: Path | None = None
    subtitles: bool = False
    intro_dir: Path | None = None
    outro_dir: Path | None = None
    copy_to_desktop: bool = True
    keep_segments: bool = False
    seed: int = 0
    speed_copies: int = 0
    # не удалять video_silent.mp4: по нему соберутся ролики на других языках
    keep_silent: bool = False
    # язык ролика в имени папки, когда рядом будут переводы: «Монголия 14.10 (RU)»
    folder_language: str = ""
    # фоновая музыка (подготовленный трек из assets/music): по кругу под всем рассказом
    music: Path | None = None
    # LLM (ключ, адрес, модель) — названия глав для описания YouTube; None — главы равными частями
    llm: tuple[str, str, str] | None = None


@dataclass
class RenderResult:
    run_dir: Path
    video_file: Path
    desktop_video: Path | None
    duration: float
    segments: int
    size_bytes: int


class RenderError(RuntimeError):
    pass


def run_render_stage(
    req: RenderRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> RenderResult:
    """Рендер — по одному на приложение: параллельный ролик в это время подбирает кадры."""
    with heavy_stage("Рендер", cancel):
        return _run_render_stage(req, cancel=cancel, on_progress=on_progress)


def _run_render_stage(
    req: RenderRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> RenderResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    run_dir = req.run_dir
    voice = run_dir / AUDIO_NAME
    if not (run_dir / TIMELINE_NAME).is_file():
        raise RenderError(f"Нет {TIMELINE_NAME}")
    if not voice.is_file():
        raise RenderError(f"Нет {AUDIO_NAME}")

    total = build_silent(req, cancel=cancel, progress=progress)
    silent = run_dir / SILENT_NAME

    srt_path: Path | None = None
    if req.subtitles:
        progress(0.82, "Субтитры…")
        srt_path = timings_to_srt(
            run_dir / TIMINGS_NAME,
            run_dir / "subs.srt",
            script_path=run_dir / "script.txt",
            run_dir=run_dir,
        )

    mixed = finish_video(req, silent=silent, voice=voice, srt_path=srt_path, dest=run_dir / "final.mp4", progress=progress)

    # видеоряд без звука ещё нужен переводам — их сборка удалит его сама
    if not req.keep_silent:
        silent.unlink(missing_ok=True)

    size = mixed.stat().st_size
    duration = probe_duration(mixed)
    desktop: Path | None = None
    if req.copy_to_desktop:
        chk_now = cp.load(run_dir)
        write_credits(run_dir, music_line=_music_credit(req.music))
        desktop = export_ready_folder(
            run_dir,
            loose=[
                chk_now.desktop_audio,
                chk_now.desktop_preview,
                chk_now.desktop_chunks,
                chk_now.desktop_timeline,
            ],
            language=req.folder_language,
        )
        if desktop is not None and req.speed_copies > 0:
            progress(0.96, f"Копии скорости: {req.speed_copies}")
            write_speed_copies(
                mixed,
                desktop,
                req.speed_copies,
                cancel=cancel,
                on_progress=lambda p, m: progress(0.96 + p * 0.04, m),
            )
        if desktop is not None:
            progress(0.98, "Главы для описания YouTube…")
            export_chapters(
                req, desktop, script=run_dir / "script.txt", timings=run_dir / TIMINGS_NAME, voice=voice, video=mixed,
                language=chk_now.language, cache=run_dir / "chapters.json",
            )
            progress(0.99, f"Папка: {desktop.name} ({size / 1e6:.1f} MB)")

    chk = cp.load(run_dir)
    cp.mark_render_done(
        run_dir,
        language=chk.language,
        voice_id=chk.voice_id,
        text_chars=chk.text_chars,
        audio_bytes=chk.audio_bytes,
        desktop_audio=chk.desktop_audio,
        word_count=chk.word_count,
        duration=duration,
        desktop_preview=chk.desktop_preview,
        chunk_count=chk.chunk_count,
        desktop_chunks=chk.desktop_chunks,
        selected_media=chk.selected_media,
        desktop_timeline=chk.desktop_timeline,
        desktop_video=str(desktop or ""),
        video_bytes=size,
    )
    cp.write_meta(
        run_dir,
        {
            "stage": "render_done",
            "duration": duration,
            "video_bytes": size,
            "desktop_video": str(desktop or ""),
            "segments": total,
        },
    )
    progress(1.0, f"Готово: {mixed.name} ({size / 1e6:.1f} MB, {duration:.1f}с)")
    return RenderResult(
        run_dir=run_dir,
        video_file=mixed,
        desktop_video=desktop,
        duration=duration,
        segments=total,
        size_bytes=size,
    )



def _music_credit(music: Path | None) -> str:
    if music is None:
        return ""
    from engine.slides.music import catalog, credit_line

    tid = next((t["id"] for t in catalog()["tracks"] if t["file"] == music.name), "")
    return credit_line(tid)


def build_silent(
    req: RenderRequest,
    *,
    cancel: CancelToken | None = None,
    progress: ProgressCb,
) -> int:
    """Видеоряд без звука под длину озвучки → run_dir/video_silent.mp4. Вернуть число сегментов.

    Порядок кадров не случайный (req.seed): повторная сборка даёт тот же ряд.
    """
    run_dir = req.run_dir
    timeline_path = run_dir / TIMELINE_NAME
    voice = run_dir / AUDIO_NAME
    raw_tl = json.loads(timeline_path.read_text(encoding="utf-8"))
    if not isinstance(raw_tl, dict) or raw_tl.get("mode") not in (
        "theme_pool",
        "strict_sync",
    ):
        raise RenderError(
            "Таймлайн старого покадрового режима. Запусти «Кадры» ещё раз."
        )

    voice_dur = probe_duration(voice)
    fade_sec = float(raw_tl.get("fade_sec") or 0.5)
    if raw_tl.get("mode") == "strict_sync":
        # Порядок строгого ряда — по тексту, его НЕ переставляем.
        block = _strict_items(run_dir, {"slots": raw_tl.get("loop_block") or []})
        if block:
            # «Первые 3 мин»: начало по тексту + блок пула, который рендерим один раз
            # и повторяем склейкой без пережатия до конца озвучки.
            loop_start = float(raw_tl.get("loop_start") or 0.0)
            early = ordered_segments(_strict_items(run_dir, raw_tl), loop_start) if loop_start > 0.2 else []
            block_dur = sum(d for _, _, d in block)
            repeats = max(1, math.ceil((voice_dur - sum(d for _, _, d in early)) / max(block_dur, 0.1)))
            segments = early + block
            order = list(range(len(early))) + [len(early) + j for j in range(len(block))] * repeats
            progress(
                0.02,
                f"Рендер: начало {len(early)} кадров по тексту, дальше блок {len(block)} кадров "
                f"({block_dur:.0f}с) × {repeats}, аудио {voice_dur:.1f}с",
            )
        else:
            segments = ordered_segments(_strict_items(run_dir, raw_tl), voice_dur)
            order = list(range(len(segments)))
            n_video = sum(1 for kind, _, _ in segments if kind == "video")
            n_image = sum(1 for kind, _, _ in segments if kind == "image")
            progress(
                0.02,
                f"Рендер: строгий ряд {n_image} картинок и {n_video} видео "
                f"({sum(d for _, _, d in segments):.0f}с) по тексту, аудио {voice_dur:.1f}с",
            )
        if not segments:
            raise RenderError("В строгом таймлайне нет кадров")
        n_video = sum(1 for kind, _, _ in segments if kind == "video")
        n_image = sum(1 for kind, _, _ in segments if kind == "image")
    else:
        videos = _resolve_list(run_dir, raw_tl.get("videos"))
        images = _resolve_list(run_dir, raw_tl.get("images"))
        if not images:
            raise RenderError("В пуле нет картинок")
        hold_sec = float(raw_tl.get("image_hold_sec") or 10.0)
        segments = reel_segments(
            images,
            videos,
            _video_durations(raw_tl.get("videos")),
            voice_dur,
            hold_sec,
            random.Random(req.seed),
            video_hold=float(raw_tl.get("video_hold_sec") or hold_sec),
        )
        n_video = sum(1 for kind, _, _ in segments if kind == "video")
        n_image = sum(1 for kind, _, _ in segments if kind == "image")
        reel_dur = sum(d for _, _, d in segments)
        copies = repeat_count(reel_dur, voice_dur)
        # Пул короче озвучки повторяем, но каждый круг в новом порядке и без одинаковых
        # кадров подряд (в том числе на стыке кругов) — повтор не бросается в глаза.
        # «одинаковые» — по виду, а не по файлу: то же фото с другого сайта тоже не встанет рядом
        groups = look_groups([p for _, p, _ in segments])
        # «крючок»: первые ~2,5 мин — самые красивые кадры пула (оценка CLIP из подбора)
        pool_items = {
            str((run_dir / str(it.get("path") or "")).resolve()): it
            for it in [*(raw_tl.get("videos") or []), *(raw_tl.get("images") or [])]
            if isinstance(it, dict)
        }
        meta = [pool_items.get(str(p.resolve()), {}) for _, p, _ in segments]
        # начало: красивейшие видео, потом фото; AI-кадров в первые минуты нет
        head = hook_first(
            [head_rank(m) for m in meta], [d for _, _, d in segments], HOOK_SEC,
            video=[k == "video" for k, _, _ in segments],
            allowed=[m.get("provider") != "ai" for m in meta],
        ) if any(m.get("beauty") or m.get("quality") for m in meta) else []
        pool_left = [i for _ in range(copies) for i in range(len(segments))]
        for i in head:
            pool_left.remove(i)
        order = head + spread(
            pool_left,
            key=lambda i: groups[segments[i][1]],
            rng=random.Random(req.seed),
            avoid_first=groups[segments[head[-1]][1]] if head else None,
        )
        progress(
            0.02,
            f"Рендер: один блок {n_image} картинок и {n_video} видео "
            f"({reel_dur:.0f}с), повтор ×{copies}, аудио {voice_dur:.1f}с",
        )

    seg_dir = run_dir / "segments"
    if seg_dir.exists():
        shutil.rmtree(seg_dir, ignore_errors=True)
    seg_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[Path, Callable[[], Path], str]] = []
    seen_v = 0
    seen_i = 0
    for i, (kind, media, dur) in enumerate(segments):
        dest = seg_dir / f"seg_{i:05d}.mp4"
        if kind == "video":
            seen_v += 1
            label = f"Видео {seen_v}/{n_video}"
            jobs.append(
                (
                    dest,
                    lambda m=media, d=dur, dest=dest: render_video_cover(
                        media=m, duration=d, dest=dest
                    ),
                    label,
                )
            )
        else:
            seen_i += 1
            label = f"Картинка {seen_i}"
            jobs.append(
                (
                    dest,
                    lambda m=media, d=dur, dest=dest, seed=req.seed + i: render_image_canvas(
                        media=m,
                        duration=d,
                        dest=dest,
                        seed=seed,
                        fade_sec=fade_sec,
                    ),
                    label,
                )
            )
    segment_files = [dest for dest, _fn, _label in jobs]
    _render_parallel(jobs, cancel=cancel, progress=progress)
    total = len(segment_files)

    progress(0.78, "Склейка ряда…")
    silent = run_dir / SILENT_NAME
    concat_demuxer([segment_files[i] for i in order], silent)
    if not req.keep_segments:
        shutil.rmtree(seg_dir, ignore_errors=True)
    return total


def edge_clips(req: RenderRequest) -> tuple[Path | None, Path | None]:
    """Интро и аутро ролика. Тот же seed — те же файлы (у всех языков одинаковые)."""
    from engine.compositor.picker import pick_intro, pick_outro

    rng = random.Random(req.seed)
    intro = pick_intro(req.intro_dir, rng) if req.intro_dir is not None and req.intro_dir.is_dir() else None
    outro = pick_outro(req.outro_dir, rng) if req.outro_dir is not None and req.outro_dir.is_dir() else None
    return intro, outro


def export_chapters(
    req: RenderRequest,
    folder: Path,
    *,
    script: Path,
    timings: Path | None,
    voice: Path,
    video: Path,
    language: str = "",
    cache: Path | None = None,
    base_text: str = "",
) -> Path | None:
    """главы.txt (и для каждой копии скорости) в папку готового ролика. base_text — оригинал (для перевода)."""
    from engine.core.chapters import CHAPTERS_NAME, read_words, write_chapters
    from engine.core.langdetect import detect_language
    from engine.slides.publish import attribution_block
    from engine.slides.speed_copies import copy_filename, copy_percents

    text = script.read_text(encoding="utf-8") if script.is_file() else ""
    intro, _outro = edge_clips(req)
    try:
        return write_chapters(
            folder,
            text=text,
            words=read_words(timings),
            voice_duration=probe_duration(voice),
            language=language,
            llm=req.llm,
            offset=probe_duration(intro) if intro is not None else 0.0,
            total=probe_duration(video),
            copies=[(copy_filename(p, CHAPTERS_NAME), p / 100) for p in copy_percents(req.speed_copies)],
            cache=cache,
            # авторы нарезок YouTube — сразу под главами: описание вставляется одним куском
            footer=attribution_block(req.run_dir, language or detect_language(text) or ""),
            preset=_preset_chapters(req.run_dir, language or detect_language(text) or ""),
            base_text=base_text,
        )
    except Exception as exc:  # без глав ролик всё равно готов
        log(f"Главы: не получилось ({exc})")
        return None


def _preset_chapters(run_dir: Path, language: str) -> list[tuple[float, str]] | None:
    """Режим «Звёзды»: главы — по героям («4:10 — Юрий Никулин»), LLM для них не нужен."""
    try:
        raw = json.loads((run_dir / TIMELINE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("variant") != "stars":
        return None
    from engine.slides.stars_media import star_chapters

    return star_chapters(raw, language) or None


def finish_video(
    req: RenderRequest,
    *,
    silent: Path,
    voice: Path,
    srt_path: Path | None,
    dest: Path,
    progress: ProgressCb,
) -> Path:
    """Видеоряд + голос → dest вместе с субтитрами, интро/аутро и подпиской — одним проходом.

    Раньше каждое из них пережимало весь ролик заново (до 4 раз на 1080p).
    Тот же seed — та же подписка и то же интро, поэтому ролики на разных языках одинаковые.
    """
    from engine.compositor.onepass import Inputs, edge_filters, join, story_audio

    intro, outro = edge_clips(req)
    sub_path = req.subscribe_path if req.subscribe and req.subscribe_path and req.subscribe_path.is_file() else None
    burn = srt_path is not None and srt_path.is_file() and has_filter("subtitles")
    music = req.music if req.music is not None and req.music.is_file() else None
    if intro is None and outro is None and sub_path is None and not burn:
        # картинку не трогаем — только звук (и мягкие субтитры, если ffmpeg без libass)
        progress(0.85, "Микс аудио…")
        return mux_av(video=silent, voice=voice, dest=dest, music=music, music_volume=MUSIC_VOLUME, srt=srt_path)

    voice_dur = probe_duration(voice)
    inputs = Inputs(first_index=2)
    filters: list[str] = []
    tmp_srt: Path | None = None
    pre = ""
    if burn:
        assert srt_path is not None
        pre, tmp_srt = subtitles_filter(srt_path)
        pre += ","
    # видеоряд ровно длиной голоса: короче — дотягиваем последним кадром
    filters.append(
        f"[0:v]scale=out_range=tv,format=yuv420p,{pre}tpad=stop_mode=clone:stop_duration=2,"
        f"trim=duration={voice_dur:.3f},setpts=PTS-STARTPTS,setsar=1[story_v]"
    )
    parts: list[str] = []
    total = voice_dur
    size = (OUT_WIDTH, OUT_HEIGHT)
    if intro is not None:
        idx, audio, dur = inputs.clip(intro)
        filters += edge_filters(idx, audio, dur, size=size, fps=OUT_FPS, label="intro")
        parts.append("[intro_v][intro_a]")
        total += dur
    if music is not None:
        # музыка по кругу под рассказом: ровно, ~−20 дБ под голосом, в конце мягко затихает
        midx = inputs.add("-stream_loop", "-1", "-i", str(music))
        filters.append(story_audio("1:a", voice_dur, label="voice_a"))
        filters.append(
            f"[{midx}:a]aformat=sample_fmts=fltp:channel_layouts=stereo,aresample=48000,"
            f"volume={MUSIC_VOLUME},atrim=duration={voice_dur:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={max(0.0, voice_dur - 3):.3f}:d=3[music_a]"
        )
        filters.append("[voice_a][music_a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[story_a]")
    else:
        filters.append(story_audio("1:a", voice_dur))
    parts.append("[story_v][story_a]")
    if outro is not None:
        idx, audio, dur = inputs.clip(outro)
        filters += edge_filters(idx, audio, dur, size=size, fps=OUT_FPS, label="outro")
        parts.append("[outro_v][outro_a]")
        total += dur
    filters += join(parts)
    v_map, a_map = "[cat_v]", "[cat_a]"
    if sub_path is not None:
        sub_idx = inputs.add("-i", str(sub_path))
        sub_filters, starts = subscribe_filters(sub_path, sub_idx, total, seed=req.seed)
        filters += sub_filters
        v_map, a_map = "[vout]", "[aout]"
        log("Подписка внизу кадра, показы на " + ", ".join(f"{t:.0f}с" for t in starts))
    what = ", ".join(x for x, on in (("субтитры", burn), ("интро", intro), ("аутро", outro), ("подписка", sub_path), ("музыка", music)) if on)
    progress(0.85, f"Сборка ролика одним проходом: {what}…")
    cmd = [
        # -reinit_filter 0: смена формата кадров посреди видеоряда не перезапускает граф
        # (перезапуск сбрасывал trim/склейку, и ролик обрывался на 2–3 минуте)
        find_ffmpeg(), "-y", "-reinit_filter", "0", "-i", str(silent), "-i", str(voice), *inputs.args,
        "-filter_complex", ";".join(filters),
        "-map", v_map, "-map", a_map,
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-t", f"{total:.3f}", "-movflags", "+faststart", str(dest),
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg(cmd, label="final")
    finally:
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)
    return dest


def _video_durations(raw_items: object) -> list[float]:
    durations: list[float] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict):
                try:
                    durations.append(float(item.get("duration") or 10.0))
                except (TypeError, ValueError):
                    durations.append(10.0)
    return durations


def reel_segments(
    images: list[Path],
    videos: list[Path],
    video_durs: list[float],
    voice_dur: float,
    hold: float,
    rng: random.Random,
    *,
    video_hold: float | None = None,
) -> list[tuple[str, Path, float]]:
    """Один проход: каждая картинка один раз, видео между ними. Дольше озвучки не кодируем.

    hold — сколько стоит фото; video_hold — потолок длины видеокуска (по умолчанию как фото).
    """
    if not images or hold <= 0 or voice_dur <= 0.2:
        return []
    pics = list(images)
    rng.shuffle(pics)
    clips: list[tuple[Path, float]] = []
    for i, path in enumerate(videos):
        dur = video_durs[i] if i < len(video_durs) else hold
        clips.append((path, max(0.4, min(float(dur), video_hold or hold))))
    rng.shuffle(clips)
    points = _insert_after(len(pics), len(clips), rng)
    segments: list[tuple[str, Path, float]] = []
    video_i = 0
    for i, pic in enumerate(pics):
        segments.append(("image", pic, hold))
        while video_i < len(clips) and video_i < len(points) and points[video_i] == i:
            path, dur = clips[video_i]
            segments.append(("video", path, dur))
            video_i += 1
    return _trim_reel(segments, voice_dur)


def ordered_segments(
    items: list[tuple[str, Path, float]],
    voice_dur: float,
) -> list[tuple[str, Path, float]]:
    """Слоты по порядку текста. Хвост добивает последний кадр, лишнее срезается."""
    if not items or voice_dur <= 0.2:
        return []
    trimmed = _trim_reel(items, voice_dur)
    acc = sum(dur for _, _, dur in trimmed)
    if trimmed and acc < voice_dur - 0.05:
        kind, path, dur = trimmed[-1]
        trimmed[-1] = (kind, path, dur + (voice_dur - acc))
    return trimmed


def repeat_count(reel_dur: float, voice_dur: float) -> int:
    """Сколько раз поставить готовый блок, чтобы закрыть озвучку."""
    if reel_dur <= 0.2 or voice_dur <= 0.2:
        return 1
    if reel_dur >= voice_dur - 0.05:
        return 1
    return max(1, math.ceil(voice_dur / reel_dur))


def _insert_after(n_images: int, n_videos: int, rng: random.Random) -> list[int]:
    """Индекс картинки, после которой встаёт ролик. До первой картинки видео нет."""
    if n_videos <= 0 or n_images <= 0:
        return []
    points: list[int] = []
    for i in range(n_videos):
        lo = int(i * n_images / n_videos)
        hi = int((i + 1) * n_images / n_videos) - 1
        hi = max(lo, min(hi, n_images - 1))
        points.append(rng.randint(lo, hi))
    return points


def _trim_reel(
    segments: list[tuple[str, Path, float]], voice_dur: float
) -> list[tuple[str, Path, float]]:
    out: list[tuple[str, Path, float]] = []
    acc = 0.0
    for kind, path, dur in segments:
        if acc >= voice_dur - 0.05:
            break
        use = min(dur, voice_dur - acc)
        if use < 0.3:
            break
        out.append((kind, path, use))
        acc += use
    return out


def _render_parallel(
    jobs: list[tuple[Path, Callable[[], Path], str]],
    *,
    cancel: CancelToken | None,
    progress: ProgressCb,
) -> None:
    """Несколько ffmpeg сразу. Качество то же, сегменты пишутся в заранее заданные файлы."""
    if not jobs:
        return
    total = len(jobs)
    done = 0
    ex = ThreadPoolExecutor(max_workers=RENDER_WORKERS)
    try:
        futs = {ex.submit(fn): label for _dest, fn, label in jobs}
        for fut in as_completed(futs):
            if cancel:
                cancel.check()
            fut.result()
            done += 1
            progress(0.05 + 0.70 * (done / total), f"{futs[fut]} ({done}/{total})")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _strict_items(run_dir: Path, raw_tl: dict) -> list[tuple[str, Path, float]]:
    slots = raw_tl.get("slots")
    if not isinstance(slots, list):
        return []
    out: list[tuple[str, Path, float]] = []
    for item in slots:
        if not isinstance(item, dict):
            continue
        path = _resolve_media(run_dir, item)
        if path is None:
            continue
        kind = "video" if item.get("kind") == "video" else "image"
        try:
            dur = float(item.get("duration") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur < 0.3:
            continue
        out.append((kind, path, dur))
    return out


def _resolve_list(run_dir: Path, raw: object) -> list[Path]:
    if not isinstance(raw, list):
        return []
    out: list[Path] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _resolve_media(run_dir, item)
        if path is not None:
            out.append(path)
    return out


def _image_durations(remain: float, hold: float) -> list[float]:
    if remain <= 0.2 or hold <= 0:
        return []
    out: list[float] = []
    left = remain
    while left > 0.2:
        d = min(hold, left)
        out.append(d)
        left -= d
    return out


def _resolve_media(run_dir: Path, item: dict) -> Path | None:
    rel = str(item.get("path") or "").strip()
    if not rel:
        return None
    path = (run_dir / rel).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
